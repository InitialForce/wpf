using BenchmarkDotNet.Attributes;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Windows.Threading;

namespace WpfMicrobenchmarks.Benchmarks;

// profile.json entry: "WindowsBase!System.Windows.Threading.Dispatcher.Invoke(class System.Action)" (cpu_pct=2.09%)
// profile.json entry: "WindowsBase!System.Windows.Threading.Dispatcher.Invoke(class System.Action,value class System.Windows.Threading.DispatcherPriority,value class System.Threading.CancellationToken,value class System.TimeSpan)" (cpu_pct=2.09%)
// Exercises: Dispatcher.Invoke fast path — same-thread + Send priority: SyncContext swap + direct callback.
//            No message pump required; fast path unconditionally taken when CheckAccess() == true.

/// <summary>
/// Benchmarks the Dispatcher.Invoke fast path (entries 2.1 + 2.2 from DESIGN-NOTES-dispatcher.md).
///
/// Design note: Dispatcher.Invoke(Action) at DispatcherPriority.Send on the Dispatcher's own
/// thread hits a fast path (Dispatcher.cs lines 577-608) that skips the operation queue entirely:
/// it swaps SynchronizationContext, calls the callback, then restores SynchronizationContext.
/// This fast path is benchmarkable without a Win32 message pump.
///
/// Threading: a dedicated STA thread is created in GlobalSetup. That thread calls
/// Dispatcher.CurrentDispatcher to auto-create a Dispatcher bound to itself. Each [Benchmark]
/// method posts a batch of StaBatch invocations to the STA thread and waits for completion.
/// BDN measures the full round-trip per batch call; per-invoke cost = measured_ns / StaBatch.
/// Cross-thread signaling (~200-500 ns) is amortized to &lt;1 ns/op at StaBatch=1024.
/// Note: BDN 0.14.* does not have [OperationsPerInvoke]; per-invoke cost is computed post-hoc.
///
/// Corpus: 64 distinct Action delegates (seeded RNG, each with a unique XOR capture).
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class DispatcherInvokeActionBenchmark : IDisposable
{
    internal const int StaBatch = 1024;
    private const int CorpusSize = 64;
    private const int CorpusMask = CorpusSize - 1;

    private Thread? _staThread;
    private Dispatcher? _dispatcher;
    private volatile bool _staRunning;
    private ManualResetEventSlim _staReady = new(false);
    private Exception? _staSetupException;

    // Single work-item state: the STA thread reads _pendingWork, executes it, signals _workDone
    private Action? _pendingWork;
    private ManualResetEventSlim _workReady = new(false);
    private ManualResetEventSlim _workDone = new(false);

    private Action[] _actions = Array.Empty<Action>();
    private int _index;

    [GlobalSetup]
    public void Setup()
    {
        // Pin to a single logical core to minimize OS scheduler variance
        // (same pattern as Gate 3 retry recommendation in bench-author-prompt.md)
        System.Diagnostics.Process.GetCurrentProcess().ProcessorAffinity = new IntPtr(1);

        var rng = new Random(42);
        _actions = new Action[CorpusSize];
        for (int i = 0; i < _actions.Length; i++)
        {
            int captured = rng.Next(1, 100_000);
            _actions[i] = MakeAction(captured);
        }
        _index = 0;

        _staThread = new Thread(StaThreadProc);
        _staThread.SetApartmentState(ApartmentState.STA);
        _staThread.IsBackground = true;
        _staThread.Name = "DispatcherInvokeBenchmark-STA";
        _staThread.Start();

        _staReady.Wait(TimeSpan.FromSeconds(10));
        if (_staSetupException != null)
            throw new InvalidOperationException("STA thread setup failed", _staSetupException);
        if (_dispatcher == null)
            throw new InvalidOperationException("STA thread did not create a Dispatcher");

        // Pre-warm: run a few batches before BDN starts measuring to stabilize JIT and STA thread state
        for (int w = 0; w < 3; w++)
            DispatchBatch(StaBatch, invokeMode: 0);
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
    }

    public void Dispose()
    {
        _staRunning = false;
        _pendingWork = null;
        _workReady.Set();
        _staThread?.Join(TimeSpan.FromSeconds(3));
        _staReady.Dispose();
        _workReady.Dispose();
        _workDone.Dispose();
    }

    // ── Benchmark methods ──────────────────────────────────────────────────────
    // Each method posts StaBatch dispatcher operations to the STA thread.
    // BDN measures total batch time; per-invoke cost = reported_ns / StaBatch.

    /// <summary>
    /// Entry 2.1: Dispatcher.Invoke(Action) — 1-arg overload.
    /// Delegates to 4-arg overload at Send priority + same thread = fast path.
    /// Batch size: StaBatch=1024. Per-invoke cost = batch_ns / 1024.
    /// </summary>
    [Benchmark(Description = "Dispatcher.Invoke(Action) — Send priority fast path (batch/1024)")]
    public void InvokeAction()
    {
        DispatchBatch(StaBatch, invokeMode: 0);
    }

    /// <summary>
    /// Entry 2.2: Dispatcher.Invoke(Action, DispatcherPriority, CancellationToken, TimeSpan) — 4-arg.
    /// Canonical fast-path entry; all simpler overloads delegate to it.
    /// Batch size: StaBatch=1024. Per-invoke cost = batch_ns / 1024.
    /// </summary>
    [Benchmark(Description = "Dispatcher.Invoke(Action,Priority,CT,Timeout) — 4-arg Send fast path (batch/1024)")]
    public void InvokeAction4Arg()
    {
        DispatchBatch(StaBatch, invokeMode: 1);
    }

    /// <summary>
    /// Negative control: direct Action() call on the STA thread (no Dispatcher overhead).
    /// Delta vs InvokeAction reveals the SynchronizationContext swap cost.
    /// Batch size: StaBatch=1024. Per-call cost = batch_ns / 1024.
    /// </summary>
    [Benchmark(Description = "negative-control: direct Action() on STA thread (no Dispatcher, batch/1024)")]
    public void NegativeControlDirectCall()
    {
        DispatchBatch(StaBatch, invokeMode: 2);
    }

    // ── STA dispatch infrastructure ────────────────────────────────────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchBatch(int batchSize, int invokeMode)
    {
        // Capture locals so lambda doesn't box 'this' fields on each call
        var dispatcher = _dispatcher!;
        var actions = _actions;
        int startIdx = _index;

        _pendingWork = invokeMode switch
        {
            0 => () =>
            {
                int idx = startIdx;
                for (int i = 0; i < batchSize; i++)
                {
                    dispatcher.Invoke(actions[idx & CorpusMask]);
                    idx++;
                }
                _index = idx;
            },
            1 => () =>
            {
                int idx = startIdx;
                for (int i = 0; i < batchSize; i++)
                {
                    dispatcher.Invoke(actions[idx & CorpusMask],
                        DispatcherPriority.Send,
                        CancellationToken.None,
                        TimeSpan.FromMilliseconds(-1));
                    idx++;
                }
                _index = idx;
            },
            _ => () =>
            {
                int idx = startIdx;
                for (int i = 0; i < batchSize; i++)
                {
                    actions[idx & CorpusMask]();
                    idx++;
                }
                _index = idx;
            }
        };

        _workDone.Reset();
        _workReady.Set();
        _workDone.Wait();
    }

    private void StaThreadProc()
    {
        try
        {
            _dispatcher = Dispatcher.CurrentDispatcher;
        }
        catch (Exception ex)
        {
            _staSetupException = ex;
            _staReady.Set();
            return;
        }

        _staRunning = true;
        _staReady.Set();

        while (_staRunning)
        {
            _workReady.Wait();
            _workReady.Reset();

            if (!_staRunning) break;

            var work = _pendingWork;
            if (work != null)
            {
                work();
                _workDone.Set();
            }
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static Action MakeAction(int captured)
    {
        int acc = captured;
        return () => { acc ^= 1; };
    }
}

// ─────────────────────────────────────────────────────────────────────────────

// profile.json entry: "WindowsBase!System.Windows.Threading.DispatcherOperation.Invoke()" (cpu_pct=2.09%)
// profile.json entry: "WindowsBase!System.Windows.Threading.DispatcherOperation.InvokeImpl()" (cpu_pct=2.09%, covered transitively)
// Exercises: DispatcherOperation.Invoke() via reflection — CulturePreservingExecutionContext.Run +
//            SynchronizationContext swap + WrappedInvoke. Proxy-only: requires internal access.
//
// Proxy relationship: DispatcherOperation.Invoke() is internal (WindowsBase). This benchmark
// accesses it via cached MethodInfo.Invoke. InvokeImpl() is covered transitively — Invoke()
// always calls InvokeImpl() via _invokeInSecurityContext callback.
// Each iteration creates a fresh DispatcherOperation via reflection to avoid status issues.

/// <summary>
/// Benchmarks DispatcherOperation.Invoke() (entries 2.4 + 2.5 from DESIGN-NOTES-dispatcher.md).
///
/// Proxy strategy (Option B variant): DispatcherOperation and its constructors are internal.
/// We access them via reflection — MethodInfo and ConstructorInfo cached in GlobalSetup.
/// A fresh DispatcherOperation is constructed per batch-element to use the full
/// _executionContext code path (vs null path taken after first Invoke on same instance).
///
/// Threading: a dedicated STA thread owns the Dispatcher. Benchmarks post batches to the STA
/// thread. Per-invoke cost = reported_ns / StaBatch.
///
/// Corpus: 64 distinct Action delegates (seeded RNG). Each op uses actions[i % 64].
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class DispatcherOperationInvokeBenchmark : IDisposable
{
    internal const int StaBatch = 256;
    private const int CorpusSize = 64;
    private const int CorpusMask = CorpusSize - 1;

    private Thread? _staThread;
    private Dispatcher? _dispatcher;
    private volatile bool _staRunning;
    private ManualResetEventSlim _staReady = new(false);
    private Exception? _staSetupException;

    private Action? _pendingWork;
    private ManualResetEventSlim _workReady = new(false);
    private ManualResetEventSlim _workDone = new(false);

    private Action[] _actions = Array.Empty<Action>();
    private int _index;

    // Reflection handles — cached once in GlobalSetup
    private ConstructorInfo? _opCtor;   // DispatcherOperation(Dispatcher, DispatcherPriority, Action)
    private MethodInfo? _opInvoke;      // DispatcherOperation.Invoke() [internal]
    private bool _reflectionAvailable;

    [GlobalSetup]
    public void Setup()
    {
        // Pin to a single logical core to minimize OS scheduler variance
        System.Diagnostics.Process.GetCurrentProcess().ProcessorAffinity = new IntPtr(1);

        var rng = new Random(42);
        _actions = new Action[CorpusSize];
        for (int i = 0; i < _actions.Length; i++)
        {
            int captured = rng.Next(1, 100_000);
            _actions[i] = MakeAction(captured);
        }
        _index = 0;

        // Cache reflection handles for DispatcherOperation (internal type in WindowsBase)
        var windowsBase = typeof(Dispatcher).Assembly;
        var opType = windowsBase.GetType(
            "System.Windows.Threading.DispatcherOperation",
            throwOnError: false);

        if (opType != null)
        {
            // Constructor: DispatcherOperation(Dispatcher, DispatcherPriority, Action)
            _opCtor = opType.GetConstructor(
                BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null,
                types: new[] { typeof(Dispatcher), typeof(DispatcherPriority), typeof(Action) },
                modifiers: null);

            // internal void Invoke()
            _opInvoke = opType.GetMethod(
                "Invoke",
                BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public,
                binder: null,
                types: Type.EmptyTypes,
                modifiers: null);

            _reflectionAvailable = _opCtor != null && _opInvoke != null;
        }

        _staThread = new Thread(StaThreadProc);
        _staThread.SetApartmentState(ApartmentState.STA);
        _staThread.IsBackground = true;
        _staThread.Name = "DispatcherOpInvokeBenchmark-STA";
        _staThread.Start();

        _staReady.Wait(TimeSpan.FromSeconds(10));
        if (_staSetupException != null)
            throw new InvalidOperationException("STA thread setup failed", _staSetupException);
        if (_dispatcher == null)
            throw new InvalidOperationException("STA thread did not create a Dispatcher");

        // Pre-warm: stabilize JIT and STA thread state before BDN measures
        if (_reflectionAvailable)
        {
            for (int w = 0; w < 3; w++)
                DispatchBatch();
        }
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
    }

    public void Dispose()
    {
        _staRunning = false;
        _pendingWork = null;
        _workReady.Set();
        _staThread?.Join(TimeSpan.FromSeconds(3));
        _staReady.Dispose();
        _workReady.Dispose();
        _workDone.Dispose();
    }

    // ── Benchmark methods ──────────────────────────────────────────────────────

    /// <summary>
    /// Entries 2.4 + 2.5 (proxy): DispatcherOperation.Invoke() via reflection.
    /// Fresh DispatcherOperation per operation; exercises full ExecutionContext path.
    /// Batch size: StaBatch=256. Per-invoke cost = batch_ns / 256.
    /// </summary>
    [Benchmark(Description = "DispatcherOperation.Invoke() proxy — reflection, fresh op per call (batch/256)")]
    public void DispatcherOperationInvoke()
    {
        if (!_reflectionAvailable) return;
        DispatchBatch();
    }

    /// <summary>
    /// Negative control: direct Action() call (no DispatcherOperation overhead).
    /// Delta vs DispatcherOperationInvoke shows cost of construction + ExecutionContext.Run.
    /// Batch size: StaBatch=256.
    /// </summary>
    [Benchmark(Description = "negative-control: direct Action() call (no DispatcherOperation, batch/256)")]
    public void NegativeControlDirectCall()
    {
        DispatchDirectBatch();
    }

    // ── STA dispatch infrastructure ────────────────────────────────────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchBatch()
    {
        var dispatcher = _dispatcher!;
        var actions = _actions;
        var opCtor = _opCtor!;
        var opInvoke = _opInvoke!;
        int startIdx = _index;
        var ctorArgs = new object?[3];
        ctorArgs[0] = dispatcher;
        ctorArgs[1] = DispatcherPriority.Normal;

        _pendingWork = () =>
        {
            int idx = startIdx;
            for (int i = 0; i < StaBatch; i++)
            {
                ctorArgs[2] = actions[idx & CorpusMask];
                idx++;
                var op = opCtor.Invoke(ctorArgs);
                opInvoke.Invoke(op, null);
            }
            _index = idx;
        };

        _workDone.Reset();
        _workReady.Set();
        _workDone.Wait();
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchDirectBatch()
    {
        var actions = _actions;
        int startIdx = _index;

        _pendingWork = () =>
        {
            int idx = startIdx;
            for (int i = 0; i < StaBatch; i++)
            {
                actions[idx & CorpusMask]();
                idx++;
            }
            _index = idx;
        };

        _workDone.Reset();
        _workReady.Set();
        _workDone.Wait();
    }

    private void StaThreadProc()
    {
        try
        {
            _dispatcher = Dispatcher.CurrentDispatcher;
        }
        catch (Exception ex)
        {
            _staSetupException = ex;
            _staReady.Set();
            return;
        }

        _staRunning = true;
        _staReady.Set();

        while (_staRunning)
        {
            _workReady.Wait();
            _workReady.Reset();

            if (!_staRunning) break;

            var work = _pendingWork;
            if (work != null)
            {
                work();
                _workDone.Set();
            }
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static Action MakeAction(int captured)
    {
        int acc = captured;
        return () => { acc ^= 1; };
    }
}
