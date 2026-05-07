using BenchmarkDotNet.Attributes;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Threading;

namespace WpfMicrobenchmarks.Benchmarks;

// profile.json entry: "WindowsBase!MS.Win32.HwndSubclass.SubclassWndProc(int,int32,int,int)" (cpu_pct=2.09%)
// profile.json entry: "WindowsBase!MS.Win32.HwndWrapper.WndProc(int,int32,int,int,bool&)" (cpu_pct=2.09%)
// Exercises: HwndWrapper.WndProc hook-dispatch loop via SendMessage from the benchmark thread;
//            HwndSubclass.SubclassWndProc null-dispatcher path (Option B from design notes).
//
// Design caveat: In production, HwndSubclass.SubclassWndProc runs on the STA UI thread where
// Dispatcher.FromThread returns a live Dispatcher. This benchmark exercises Option B
// (Dispatcher.FromThread returns null on the owning STA helper thread) because injecting a full
// Dispatcher pump into a BDN run introduces more variance than signal. The HwndWrapper.WndProc
// hook-dispatch loop is fully exercised regardless of Dispatcher presence.

/// <summary>
/// Benchmarks the Win32 WndProc chain managed wrappers: HwndWrapper.WndProc (hook-dispatch
/// loop) and HwndSubclass.SubclassWndProc (entry point for subclassed messages).
///
/// Setup: a dedicated STA helper thread creates an HwndWrapper (message-only window) via
/// reflection, then runs a PeekMessage/TranslateMessage/DispatchMessage pump. Benchmark
/// methods call SendMessage from the BDN thread to drive the WndProc chain. The owning
/// thread processes each message synchronously during SendMessage before returning.
///
/// Access strategy: HwndWrapper and HwndSubclass are internal to MS.Win32 (WindowsBase).
/// We drive them indirectly through the public Win32 SendMessage API — the WndProc chain
/// is invoked by the OS on the owning thread during each SendMessage call. We register
/// hook delegates via the public AddHook method obtained via reflection.
///
/// Corpus: 64 WM_USER+i message IDs. BDN rotates through them to prevent branch
/// prediction monoculture on the msg dispatch path.
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class HwndWin32Benchmark : IDisposable
{
    // Win32 constants
    private const int HWND_MESSAGE = -3;
    private const int WM_USER = 0x0400;

    // Reflection-obtained members
    private Type? _hwndWrapperType;
    private object? _hwndWrapper;      // HwndWrapper instance (1 hook — fast path)
    private object? _hwndWrapper4;     // HwndWrapper instance (4 hooks — loop body)
    private IntPtr _hwnd;              // Handle from _hwndWrapper.Handle
    private IntPtr _hwnd4;             // Handle from _hwndWrapper4.Handle

    // STA host thread and its pump control
    private Thread? _staThread;
    private volatile bool _staRunning;
    private ManualResetEventSlim _staReady = new(false);
    private Exception? _staSetupException;

    // Corpus of 64 WM_USER+i message IDs
    private int[] _messages = Array.Empty<int>();
    private int _index;

    [GlobalSetup]
    public void Setup()
    {
        _messages = new int[64];
        var rng = new Random(42);
        for (int i = 0; i < _messages.Length; i++)
        {
            // WM_USER+1 .. WM_USER+64 (avoid WM_USER+0 which is sometimes special)
            _messages[i] = WM_USER + 1 + (rng.Next(63));
        }

        _index = 0;

        // Spin an STA thread that creates HwndWrapper instances and runs a message pump
        _staThread = new Thread(StaThreadProc);
        _staThread.SetApartmentState(ApartmentState.STA);
        _staThread.IsBackground = true;
        _staThread.Name = "HwndWin32Benchmark-STA";
        _staThread.Start();

        // Wait for the STA thread to finish HWND creation (or fail)
        _staReady.Wait(TimeSpan.FromSeconds(10));
        if (_staSetupException != null)
            throw new InvalidOperationException("STA setup failed", _staSetupException);
        if (_hwnd == IntPtr.Zero || _hwnd4 == IntPtr.Zero)
            throw new InvalidOperationException("STA thread did not produce valid HWNDs");
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
    }

    public void Dispose()
    {
        _staRunning = false;
        // Post WM_QUIT to unblock the pump
        if (_hwnd != IntPtr.Zero)
            PostMessage(_hwnd, 0x0012 /* WM_QUIT */, IntPtr.Zero, IntPtr.Zero);
        _staThread?.Join(TimeSpan.FromSeconds(3));
        _staReady.Dispose();
    }

    // ── Benchmark methods ──────────────────────────────────────────────────────

    /// <summary>
    /// Hot path: SendMessage to HwndWrapper with 1 registered hook.
    /// Exercises HwndWrapper.WndProc fast path (1-hook WeakReferenceList iteration).
    /// </summary>
    [Benchmark(Description = "HwndWrapper.WndProc — 1 hook (fast path)")]
    public IntPtr WndProc1Hook()
    {
        int msg = _messages[_index & 63];
        _index++;
        return SendMessage(new HandleRef(null, _hwnd), msg, IntPtr.Zero, IntPtr.Zero);
    }

    /// <summary>
    /// 4-hook variant: exercises the WeakReferenceList loop body with more iterations.
    /// </summary>
    [Benchmark(Description = "HwndWrapper.WndProc — 4 hooks (loop body)")]
    public IntPtr WndProc4Hooks()
    {
        int msg = _messages[_index & 63];
        _index++;
        return SendMessage(new HandleRef(null, _hwnd4), msg, IntPtr.Zero, IntPtr.Zero);
    }

    /// <summary>
    /// Negative control: call DefWindowProc directly (bypasses managed WndProc chain
    /// and HwndSubclass entirely). Reveals overhead of the managed hook-dispatch layer.
    /// </summary>
    [Benchmark(Description = "negative-control: DefWindowProc direct (bypass managed WndProc)")]
    public IntPtr NegativeControlDefWndProc()
    {
        int msg = _messages[_index & 63];
        _index++;
        return DefWindowProc(_hwnd, msg, IntPtr.Zero, IntPtr.Zero);
    }

    // ── STA helper thread ──────────────────────────────────────────────────────

    private void StaThreadProc()
    {
        try
        {
            var windowsBase = typeof(System.Windows.Threading.Dispatcher).Assembly;
            _hwndWrapperType = windowsBase.GetType("MS.Win32.HwndWrapper", throwOnError: true)!;

            // Create HwndWrapper with 1 hook
            HwndWrapperHookDelegate hook1 = NoopHook;
            _hwndWrapper = CreateHwndWrapperViaReflection(_hwndWrapperType!, new[] { (object)hook1 });
            _hwnd = GetHandleViaReflection(_hwndWrapper);

            // Create HwndWrapper with 4 hooks
            HwndWrapperHookDelegate hook2a = NoopHook;
            HwndWrapperHookDelegate hook2b = NoopHook;
            HwndWrapperHookDelegate hook2c = NoopHook;
            HwndWrapperHookDelegate hook2d = NoopHook;
            _hwndWrapper4 = CreateHwndWrapperViaReflection(_hwndWrapperType!,
                new object[] { hook2a, hook2b, hook2c, hook2d });
            _hwnd4 = GetHandleViaReflection(_hwndWrapper4);
        }
        catch (Exception ex)
        {
            _staSetupException = ex;
            _staReady.Set();
            return;
        }

        _staRunning = true;
        _staReady.Set();

        // Run a minimal PeekMessage pump so SendMessage calls from other threads are processed
        MSG msg = default;
        while (_staRunning)
        {
            // Block waiting for messages (GetMessage) but wake on pump shutdown
            int result = GetMessage(out msg, IntPtr.Zero, 0, 0);
            if (result <= 0) break; // WM_QUIT or error
            TranslateMessage(ref msg);
            DispatchMessage(ref msg);
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static object CreateHwndWrapperViaReflection(Type hwndWrapperType, object[] hookDelegates)
    {
        // HwndWrapperHook is an internal delegate type; we must create instances of it
        var hookDelegateType = hwndWrapperType.Assembly.GetType("MS.Win32.HwndWrapperHook", throwOnError: true)!;

        // Convert our typed delegates to HwndWrapperHook instances
        var hooks = Array.CreateInstance(hookDelegateType, hookDelegates.Length);
        for (int i = 0; i < hookDelegates.Length; i++)
        {
            // Wrap the hook delegate in the expected HwndWrapperHook delegate type
            var wrapped = Delegate.CreateDelegate(hookDelegateType, hookDelegates[i], "Invoke");
            hooks.SetValue(wrapped, i);
        }

        // HwndWrapper(classStyle, style, exStyle, x, y, w, h, name, parent, hooks[])
        // Use message-only window: parent = HWND_MESSAGE (-3 as IntPtr)
        var ctor = hwndWrapperType.GetConstructors(
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)[0];

        return ctor.Invoke(new object[]
        {
            0,          // classStyle
            0,          // style
            0,          // exStyle
            0, 0,       // x, y
            0, 0,       // w, h
            "bench",    // name
            new IntPtr(HWND_MESSAGE),  // parent (message-only window)
            hooks       // hooks array
        });
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static IntPtr GetHandleViaReflection(object hwndWrapper)
    {
        var prop = hwndWrapper.GetType().GetProperty("Handle",
            BindingFlags.Public | BindingFlags.Instance)!;
        return (IntPtr)prop.GetValue(hwndWrapper)!;
    }

    // Typed delegate matching HwndWrapperHook signature, used as hook in benchmark
    private delegate IntPtr HwndWrapperHookDelegate(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled);

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static IntPtr NoopHook(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        // No-op hook: do not set handled=true so message proceeds through chain
        return IntPtr.Zero;
    }

    // ── Win32 P/Invoke ─────────────────────────────────────────────────────────

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public int ptX, ptY;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(HandleRef hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern int GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
}
