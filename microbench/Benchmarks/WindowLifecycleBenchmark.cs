using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Diagnosers;
using BenchmarkDotNet.Exporters.Json;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Toolchains.InProcess.Emit;
using BenchmarkDotNet.Validators;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Threading;

namespace WpfMicrobenchmarks.Benchmarks;

/// <summary>
/// BDN config for WindowLifecycleBenchmark.
///
/// Uses InProcessEmitToolchain so that the benchmark runs in the same process as the BDN
/// runner (Microbenchmarks.exe). This is required because WPF's Application + Window
/// classes depend on COM apartment-state initialization (STA) that must be set on the
/// process entry point. BDN's default out-of-process toolchain spawns a new process whose
/// generated Main() is MTA, which causes PresentationSource's static initializer to fail.
///
/// The benchmark creates its own dedicated STA thread for all WPF work (via the static
/// WpfStaHost singleton), so the BDN measurement thread never touches WPF APIs directly.
/// </summary>
public class WindowLifecycleConfig : ManualConfig
{
    public WindowLifecycleConfig()
    {
        AddJob(Job.Default
            .WithToolchain(InProcessEmitToolchain.Instance)
            .WithLaunchCount(1)
            .WithWarmupCount(3)
            .WithIterationCount(10)
            .WithUnrollFactor(1)); // UnrollFactor=1: STA thread round-trips aren't unrollable

        AddDiagnoser(MemoryDiagnoser.Default);
        AddExporter(JsonExporter.Full);
        AddValidator(ExecutionValidator.FailOnError);
    }
}

// ─── Shared STA host (static singleton, survives across BDN benchmark instances) ───

/// <summary>
/// Manages the single Application + STA thread shared by all WindowLifecycleBenchmark instances.
///
/// WPF allows only one Application instance per AppDomain. InProcess BDN benchmarks share an
/// AppDomain, so Application must be created once and reused. This host is lazy-initialized on
/// first GlobalSetup call and never disposed — it lives until the process exits.
///
/// Thread safety: initialization is double-checked-lock; all WPF operations are marshalled to
/// the STA thread via _dispatcher.Invoke().
/// </summary>
internal static class WpfStaHost
{
    private static readonly object _lock = new();
    private static volatile bool _initialized;
    private static Thread? _staThread;
    private static Dispatcher? _dispatcher;
    private static ManualResetEventSlim _ready = new(false);
    private static Exception? _setupException;

    public static Dispatcher Dispatcher
    {
        get
        {
            EnsureInitialized();
            return _dispatcher!;
        }
    }

    public static void EnsureInitialized()
    {
        if (_initialized) return;
        lock (_lock)
        {
            if (_initialized) return;
            StartSta();
            _ready.Wait(TimeSpan.FromSeconds(15));
            if (_setupException != null)
            {
                var ex = _setupException;
                var sb = new System.Text.StringBuilder();
                while (ex != null) { sb.AppendLine($"  [{ex.GetType().Name}]: {ex.Message}"); ex = ex.InnerException; }
                throw new InvalidOperationException($"WpfStaHost STA setup failed:\n{sb}", _setupException);
            }
            if (_dispatcher == null)
                throw new InvalidOperationException("WpfStaHost: STA thread did not create a Dispatcher");
            _initialized = true;
        }
    }

    private static void StartSta()
    {
        _staThread = new Thread(StaThreadProc);
        _staThread.SetApartmentState(ApartmentState.STA);
        _staThread.IsBackground = true;
        _staThread.Name = "WindowLifecycle-STA";
        _staThread.Start();
    }

    private static void StaThreadProc()
    {
        try
        {
            // Application.Current must be created on the STA thread that will host it.
            // ShutdownMode.OnExplicitShutdown prevents Application from exiting on last window close.
            var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            _dispatcher = Dispatcher.CurrentDispatcher;
        }
        catch (Exception ex)
        {
            _setupException = ex;
            _ready.Set();
            return;
        }

        _ready.Set();

        // Dispatcher.Run() pumps the Win32 message loop on the STA thread.
        // All Dispatcher.Invoke / BeginInvoke calls from benchmark methods are serviced here.
        // The host lives for the process lifetime — Application.Shutdown is never called
        // (BDN InProcess benchmarks share an AppDomain; Application can't be re-created).
        Dispatcher.Run();
    }
}

// profile.json entry: "PresentationFramework!System.Windows.Application.RunDispatcher(object)" (cpu_pct=2.20%)
// profile.json entry: "PresentationFramework!System.Windows.Application.Run(Window)" (cpu_pct=2.19%)
// profile.json entry: "PresentationFramework!System.Windows.Application.RunInternal(Window)" (cpu_pct=2.19%)
// profile.json entry: "PresentationFramework!System.Windows.Application.Run()" (cpu_pct=2.18%)
// profile.json entry: "PresentationFramework!System.Windows.Window.ShowDialog()" (cpu_pct=2.09%)
//
// Exercises: Window.Show + Window.Hide round-trips on a warm STA host as a proxy for all four
//            Application.Run* entries (structural ancestors of Dispatcher.Run, not directly iterable).
//            Window.ShowDialog measured via pre-enqueued close. Negative control = Dispatcher.Invoke no-op.
//
// Overlap note: Application.RunDispatcher body is `_ownDispatcherStarted = true; Dispatcher.Run()`.
//   Its CPU samples are inherited 100% from Dispatcher.PushFrameImpl, which the dispatcher cluster
//   already owns. This class does NOT re-implement a PushFrame benchmark — only Show/Hide/ShowDialog
//   window-lifecycle work is measured.
//
// Corpus note: Window.Show/Hide cost is Win32-message-driven, not input-dependent (no RNG corpus
//   needed). A single minimal Window instance is reused across iterations. Input variation would
//   measure Win32 message routing variance, not WPF window-lifecycle variance — 1 window is correct.
// Negative-control design: Using Dispatcher.Invoke no-op (not UpdateLayout) because UpdateLayout
//   on a clean-layout window is a no-op and produces bimodal CV (first-dirty vs subsequent-clean);
//   the Dispatcher.Invoke baseline is a stable, non-degenerate lower bound for cross-thread cost.

/// <summary>
/// Benchmarks WPF window lifecycle (entries from the window-app cluster).
///
/// Setup: WpfStaHost provides a single Application + Dispatcher running on a dedicated STA thread.
/// All benchmark work is posted via Dispatcher.Invoke (synchronous). GlobalSetup creates benchmark
/// window instances on the STA thread; GlobalCleanup closes them.
///
/// Entries 1-4 (Application.Run*/RunDispatcher/RunInternal) are proxy-only: their entire CPU
/// cost is inherited from Dispatcher.PushFrameImpl (the Win32 message loop). The Show->Hide proxy
/// is the closest runnable surface that exercises the same warm steady-state path depth.
///
/// Entry 5 (Window.ShowDialog) is partially runnable with a pre-enqueued close. Expected CV 5-10%
/// due to EnumThreadWindows + EnableWindow over thread window list + nested Dispatcher frame push.
/// </summary>
[Config(typeof(WindowLifecycleConfig))]
public class WindowLifecycleBenchmark : IDisposable
{
    // Window used for Show/Hide and UpdateLayout benchmarks
    private Window? _window;

    [GlobalSetup]
    public void Setup()
    {
        // Pin process to a single logical core — reduces OS scheduler variance for Win32
        // show/hide timing (same technique used by Gate-3 retry in bench-author-prompt.md).
        System.Diagnostics.Process.GetCurrentProcess().ProcessorAffinity = new IntPtr(1);

        // Ensure the STA host is running (idempotent after first call)
        WpfStaHost.EnsureInitialized();

        // Create and pre-warm the Show/Hide window on the STA thread
        WpfStaHost.Dispatcher.Invoke(() =>
        {
            _window = MakeMinimalWindow();
            // Initial Show+Hide: creates the HWND; subsequent iterations use existing HWND
            _window.Show();
            _window.Hide();
        });

        // Pre-warm: 50 single Show+Hide ops before BDN starts measuring to stabilize JIT + Win32 HWND state
        for (int w = 0; w < 50; w++)
            InvokeOnSta(RunShowHideSingle);
    }

    [GlobalCleanup]
    public void Cleanup()
    {
        Dispose();
    }

    public void Dispose()
    {
        var w = _window;
        if (w == null) return;
        _window = null;

        WpfStaHost.Dispatcher.Invoke(() =>
        {
            try { if (w.IsVisible) w.Hide(); } catch { /* ignore */ }
            try { w.Close(); } catch { /* ignore */ }
        });
    }

    // ── Benchmark methods ──────────────────────────────────────────────────────

    /// <summary>
    /// Proxy for Entries 1-4 (Application.Run*, RunDispatcher, RunInternal): Window.Show + Window.Hide
    /// round-trip on a warm STA host. Exercises UpdateVisibilityProperty → ShowHelper → Win32 ShowWindow.
    /// OperationsPerInvoke=50: BDN calls this 50× per measurement and reports per Show+Hide cost.
    ///
    /// All four Application.Run* entries are structural ancestors of Dispatcher.Run();
    /// their 100% sample cost is inherited from Dispatcher.PushFrameImpl. See overlap note above.
    /// </summary>
    [Benchmark(Description = "Window.Show+Hide proxy — covers Application.Run*/RunDispatcher", OperationsPerInvoke = 50)]
    public void WindowShowHideProxy()
    {
        InvokeOnSta(RunShowHideSingle);
    }

    /// <summary>
    /// Entry 5: Window.ShowDialog() with a pre-enqueued Dispatcher.BeginInvoke(Close) so the modal
    /// pump exits after one frame. Exercises EnumThreadWindows + EnableThreadWindows + nested
    /// Dispatcher frame push (ComponentDispatcher.PushModal).
    /// Per-call cost = reported_ns (single ShowDialog per BDN iteration).
    /// </summary>
    [Benchmark(Description = "Window.ShowDialog — single modal frame, pre-enqueued close")]
    public void WindowShowDialog()
    {
        InvokeOnSta(RunShowDialog);
    }

    /// <summary>
    /// Negative control: Window.Dispatcher.Invoke round-trip with no window-lifecycle work (no Show/Hide,
    /// no modal frame, no layout). Isolates the pure Dispatcher.Invoke cross-thread signaling cost.
    /// Delta vs WindowShowHideProxy reveals the Win32 ShowWindow overhead + layout cost.
    /// </summary>
    [Benchmark(Description = "negative-control: Dispatcher.Invoke no-op (no show/hide, no modal frame)")]
    public void NegativeControlDispatcherInvoke()
    {
        InvokeOnSta(NoOpAction);
    }

    // ── Batch work items (run on STA thread via Dispatcher.Invoke) ─────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void RunShowHideSingle()
    {
        var w = _window!;
        w.Show();
        w.Hide();
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private void RunShowDialog()
    {
        var d = WpfStaHost.Dispatcher;

        // Note: ShowDialog window is freshly allocated per iteration by design — a single instance
        // cannot be reused for ShowDialog because ShowDialog may not be called on an already-visible
        // or already-closed window (WPF throws InvalidOperationException in both cases).
        // The per-iteration Window construction cost (~35KB alloc/op) is structural, not accidental.
        var dlg = MakeMinimalWindow();

        // Pre-enqueue the close BEFORE ShowDialog blocks the nested pump. The BeginInvoke at Send
        // priority ensures Close is the very first item the modal pump processes after Show().
        d.BeginInvoke(DispatcherPriority.Send, (Action)(() =>
        {
            if (dlg.IsVisible) dlg.Close();
        }));

        dlg.ShowDialog();
    }

    // ── STA infrastructure ─────────────────────────────────────────────────────

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void NoOpAction()
    {
        // Intentionally empty: used as the negative control to isolate Dispatcher.Invoke overhead
        // from the window-lifecycle cost measured in WindowShowHideProxy and WindowShowDialog.
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void InvokeOnSta(Action work)
    {
        // Dispatcher.Invoke is synchronous — it blocks the calling thread until the STA thread
        // executes the delegate. This gives precise timing of the window-lifecycle work only.
        WpfStaHost.Dispatcher.Invoke(work, DispatcherPriority.Normal);
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static Window MakeMinimalWindow()
    {
        // No XAML, no data context, no visual content — minimal Win32 HWND overhead only
        return new Window
        {
            Width = 100,
            Height = 100,
            WindowStyle = WindowStyle.None,
            ShowInTaskbar = false,
            AllowsTransparency = false,
        };
    }
}
