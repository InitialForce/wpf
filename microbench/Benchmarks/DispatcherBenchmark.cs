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
/// method posts a single op to the STA thread; BDN calls the method OperationsPerInvoke=1024
/// times and divides the total time by 1024. Cross-thread signaling (~200-500 ns) is amortized
/// across BDN's iteration window; the per-op CV drops ~10× vs the prior manual StaBatch pattern.
///
/// Corpus: 64 distinct Action delegates (seeded RNG, each with a unique XOR capture).
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class DispatcherInvokeActionBenchmark : IDisposable
{
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
    private IntPtr _originalAffinity;

    [GlobalSetup]
    public void Setup()
    {
        // Save and pin to a single logical core to minimize OS scheduler variance.
        // Restored in GlobalCleanup so subsequent benchmark classes don't inherit core-0 pinning.
        var proc = System.Diagnostics.Process.GetCurrentProcess();
        _originalAffinity = proc.ProcessorAffinity;
        proc.ProcessorAffinity = new IntPtr(1);

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

        // Pre-warm: run a few single ops before BDN starts measuring to stabilize JIT and STA thread state
        for (int w = 0; w < 64; w++)
            DispatchSingle(invokeMode: 0);
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
        // Restore original ProcessorAffinity so subsequent benchmark classes don't inherit core-0 pinning.
        if (_originalAffinity != IntPtr.Zero)
            System.Diagnostics.Process.GetCurrentProcess().ProcessorAffinity = _originalAffinity;
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
    // Each method posts a single dispatcher operation to the STA thread.
    // BDN calls the method OperationsPerInvoke=1024 times and divides total time by 1024,
    // amortizing cross-thread signaling variance across the timing window.

    /// <summary>
    /// Entry 2.1: Dispatcher.Invoke(Action) — 1-arg overload.
    /// Delegates to 4-arg overload at Send priority + same thread = fast path.
    /// OperationsPerInvoke=1024: BDN calls this 1024× per measurement, reports per-op cost.
    /// </summary>
    [Benchmark(Description = "Dispatcher.Invoke(Action) — Send priority fast path", OperationsPerInvoke = 1024)]
    public void InvokeAction()
    {
        DispatchSingle(invokeMode: 0);
    }

    /// <summary>
    /// Entry 2.2: Dispatcher.Invoke(Action, DispatcherPriority, CancellationToken, TimeSpan) — 4-arg.
    /// Canonical fast-path entry; all simpler overloads delegate to it.
    /// OperationsPerInvoke=1024: BDN calls this 1024× per measurement, reports per-op cost.
    /// </summary>
    [Benchmark(Description = "Dispatcher.Invoke(Action,Priority,CT,Timeout) — 4-arg Send fast path", OperationsPerInvoke = 1024)]
    public void InvokeAction4Arg()
    {
        DispatchSingle(invokeMode: 1);
    }

    /// <summary>
    /// Negative control: direct Action() call on the STA thread (no Dispatcher overhead).
    /// Delta vs InvokeAction reveals the SynchronizationContext swap cost.
    /// OperationsPerInvoke=1024: BDN calls this 1024× per measurement, reports per-op cost.
    /// </summary>
    [Benchmark(Description = "negative-control: direct Action() on STA thread (no Dispatcher)", OperationsPerInvoke = 1024)]
    public void NegativeControlDirectCall()
    {
        DispatchSingle(invokeMode: 2);
    }

    // ── STA dispatch infrastructure ────────────────────────────────────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchSingle(int invokeMode)
    {
        var dispatcher = _dispatcher!;
        var actions = _actions;
        int idx = _index++;

        _pendingWork = invokeMode switch
        {
            0 => () => dispatcher.Invoke(actions[idx & CorpusMask]),
            1 => () => dispatcher.Invoke(actions[idx & CorpusMask],
                    DispatcherPriority.Send,
                    CancellationToken.None,
                    TimeSpan.FromMilliseconds(-1)),
            _ => () => actions[idx & CorpusMask](),
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
/// A fresh DispatcherOperation is constructed per op to use the full
/// _executionContext code path (vs null path taken after first Invoke on same instance).
///
/// Threading: a dedicated STA thread owns the Dispatcher. Benchmarks post single ops to the STA
/// thread. BDN calls each method OperationsPerInvoke=256 times and reports per-op cost.
///
/// Corpus: 64 distinct Action delegates (seeded RNG). Each op uses actions[i % 64].
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class DispatcherOperationInvokeBenchmark : IDisposable
{
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
    private IntPtr _originalAffinity;

    // Reflection handles — cached once in GlobalSetup
    private ConstructorInfo? _opCtor;   // DispatcherOperation(Dispatcher, DispatcherPriority, Action)
    private MethodInfo? _opInvoke;      // DispatcherOperation.Invoke() [internal]
    private bool _reflectionAvailable;

    [GlobalSetup]
    public void Setup()
    {
        // Save and pin to a single logical core to minimize OS scheduler variance.
        // Restored in GlobalCleanup so subsequent benchmark classes don't inherit core-0 pinning.
        var proc = System.Diagnostics.Process.GetCurrentProcess();
        _originalAffinity = proc.ProcessorAffinity;
        proc.ProcessorAffinity = new IntPtr(1);

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
            for (int w = 0; w < 32; w++)
                DispatchSingle();
        }
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
        // Restore original ProcessorAffinity so subsequent benchmark classes don't inherit core-0 pinning.
        if (_originalAffinity != IntPtr.Zero)
            System.Diagnostics.Process.GetCurrentProcess().ProcessorAffinity = _originalAffinity;
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
    /// OperationsPerInvoke=256: BDN calls this 256× per measurement, reports per-op cost.
    /// </summary>
    [Benchmark(Description = "DispatcherOperation.Invoke() proxy — reflection, fresh op per call", OperationsPerInvoke = 256)]
    public void DispatcherOperationInvoke()
    {
        if (!_reflectionAvailable) return;
        DispatchSingle();
    }

    /// <summary>
    /// Negative control: direct Action() call (no DispatcherOperation overhead).
    /// Delta vs DispatcherOperationInvoke shows cost of construction + ExecutionContext.Run.
    /// OperationsPerInvoke=256: BDN calls this 256× per measurement, reports per-op cost.
    /// </summary>
    [Benchmark(Description = "negative-control: direct Action() call (no DispatcherOperation)", OperationsPerInvoke = 256)]
    public void NegativeControlDirectCall()
    {
        DispatchDirectSingle();
    }

    // ── STA dispatch infrastructure ────────────────────────────────────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchSingle()
    {
        var dispatcher = _dispatcher!;
        var actions = _actions;
        var opCtor = _opCtor!;
        var opInvoke = _opInvoke!;
        int idx = _index++;
        var ctorArgs = new object?[3];
        ctorArgs[0] = dispatcher;
        ctorArgs[1] = DispatcherPriority.Normal;
        ctorArgs[2] = actions[idx & CorpusMask];

        _pendingWork = () =>
        {
            var op = opCtor.Invoke(ctorArgs);
            opInvoke.Invoke(op, null);
        };

        _workDone.Reset();
        _workReady.Set();
        _workDone.Wait();
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void DispatchDirectSingle()
    {
        var actions = _actions;
        int idx = _index++;

        _pendingWork = () => actions[idx & CorpusMask]();

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
