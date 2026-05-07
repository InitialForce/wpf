using BenchmarkDotNet.Attributes;
using System.Reflection;
using System.Runtime.CompilerServices;

namespace WpfMicrobenchmarks.Benchmarks;

// profile.json entry: "WindowsBase!System.Windows.Threading.ExceptionWrapper.TryCatchWhen(class System.Object,class System.Delegate,class System.Object,int32,class System.Delegate)" (cpu_pct=2.09%)
// profile.json entry: "WindowsBase!System.Windows.Threading.ExceptionWrapper.InternalRealCall(class System.Delegate,class System.Object,int32)" (cpu_pct=2.09%)
// Exercises: ExceptionWrapper.TryCatchWhen -> InternalRealCall via reflection-cached delegate; numArgs=0 (Action path) and numArgs=1 (DispatcherOperationCallback path)

/// <summary>
/// Benchmarks ExceptionWrapper.TryCatchWhen which is an internal WPF class
/// used to wrap every DispatcherOperation callback invocation.
///
/// Access strategy: reflection (Option B from design notes) — instantiate via
/// Activator.CreateInstance(nonPublic:true) and invoke via a cached closed
/// Func&lt;&gt; delegate built once in GlobalSetup. The closed-delegate approach
/// binds the instance in GlobalSetup so per-call overhead is ~5 ns rather than
/// the ~100 ns cost of MethodInfo.Invoke on each iteration.
///
/// Corpus: 64 pre-allocated Action delegates (numArgs=0 path) and 64
/// DispatcherOperationCallback delegates (numArgs=1 path), each with a distinct
/// integer capture so the JIT cannot trivially eliminate variation.
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class ExceptionWrapperBenchmark
{
    // Closed delegate: instance bound at GlobalSetup time.
    // TryCatchWhen(object source, Delegate callback, object args, int numArgs, Delegate catchHandler)
    // Func params: (source, callback, args, numArgs, catchHandler) — 'this' is bound.
    private Func<object?, Delegate, object?, int, Delegate?, object?>? _tryCatchWhen;

    // numArgs=0 corpus: 64 Action delegates, each capturing a different int
    private Action[] _actions = Array.Empty<Action>();

    // numArgs=1 corpus: 64 DispatcherOperationCallback delegates with distinct captures
    private System.Windows.Threading.DispatcherOperationCallback[] _docCallbacks =
        Array.Empty<System.Windows.Threading.DispatcherOperationCallback>();

    // Index into corpus, advanced per benchmark iteration to avoid branch prediction monoculture
    private int _index;

    [GlobalSetup]
    public void Setup()
    {
        // Locate the internal ExceptionWrapper type in WindowsBase
        var windowsBase = typeof(System.Windows.Threading.Dispatcher).Assembly;
        var wrapperType = windowsBase.GetType(
            "System.Windows.Threading.ExceptionWrapper",
            throwOnError: true)!;

        // ExceptionWrapper has an internal parameterless ctor — use nonPublic:true
        var wrapper = Activator.CreateInstance(wrapperType, nonPublic: true)!;

        // Build a CLOSED delegate for TryCatchWhen: bind the instance so the Func<>
        // signature drops the 'this' parameter.
        // (Open delegates fail for non-public types because CLR rejects object→internalType coercion.)
        var mi = wrapperType.GetMethod(
            "TryCatchWhen",
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)!;

        _tryCatchWhen = (Func<object?, Delegate, object?, int, Delegate?, object?>)
            Delegate.CreateDelegate(
                typeof(Func<object?, Delegate, object?, int, Delegate?, object?>),
                wrapper,    // bind instance — closed delegate
                mi);

        // Build action corpus (numArgs=0 path): 64 distinct lambdas
        var rng = new Random(42);
        _actions = new Action[64];
        for (int i = 0; i < _actions.Length; i++)
        {
            int captured = rng.Next(1, 10_000);
            _actions[i] = MakeAction(captured);
        }

        // Build DispatcherOperationCallback corpus (numArgs=1 path): 64 distinct lambdas
        _docCallbacks = new System.Windows.Threading.DispatcherOperationCallback[64];
        for (int i = 0; i < _docCallbacks.Length; i++)
        {
            int captured = rng.Next(1, 10_000);
            _docCallbacks[i] = MakeDocCallback(captured);
        }

        _index = 0;
    }

    /// <summary>
    /// Hot path: TryCatchWhen with numArgs=0 (Action fast path in InternalRealCall).
    /// This is the highest-CPU entry in the misc cluster.
    /// </summary>
    [Benchmark(Description = "TryCatchWhen numArgs=0 Action path")]
    public object? TryCatchWhenAction()
    {
        // Rotate through corpus to prevent constant-folding
        var action = _actions[_index & 63];
        _index++;
        // Closed delegate — instance already bound; pass (source, callback, args, numArgs, catchHandler)
        return _tryCatchWhen!(null, action, null, 0, null);
    }

    /// <summary>
    /// Second hot path: TryCatchWhen with numArgs=1 (DispatcherOperationCallback fast path).
    /// </summary>
    [Benchmark(Description = "TryCatchWhen numArgs=1 DispatcherOperationCallback path")]
    public object? TryCatchWhenDoc()
    {
        var cb = _docCallbacks[_index & 63];
        _index++;
        object state = _index; // box an int as the single arg
        // Closed delegate — instance already bound; pass (source, callback, args, numArgs, catchHandler)
        return _tryCatchWhen!(null, cb, state, 1, null);
    }

    /// <summary>
    /// Negative control: call the Action delegate directly via DynamicInvoke, which
    /// bypasses InternalRealCall's type-check fast path entirely.
    /// Reveals the overhead of the TryCatchWhen dispatch wrapper vs. raw invocation.
    /// </summary>
    [Benchmark(Description = "negative-control: DynamicInvoke bypass (no ExceptionWrapper)")]
    public object? NegativeControlDynamicInvoke()
    {
        var action = _actions[_index & 63];
        _index++;
        return action.DynamicInvoke();
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static Action MakeAction(int captured)
    {
        // Use a mutable accumulator so the JIT cannot eliminate the lambda body
        int acc = captured;
        return () => { acc ^= 1; };
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static System.Windows.Threading.DispatcherOperationCallback MakeDocCallback(int captured)
    {
        int acc = captured;
        return (state) =>
        {
            acc ^= (state?.GetHashCode() ?? 0);
            return null;
        };
    }
}
