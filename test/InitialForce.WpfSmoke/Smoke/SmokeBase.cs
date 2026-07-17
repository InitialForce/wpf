using NUnit.Framework;
using System;
using System.Threading;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Shared scaffolding for the InitialForce.WpfSmoke test suite.
/// Provides STA thread setup and common helpers used across smoke scenarios.
/// </summary>
[TestFixture]
public abstract class SmokeBase
{
    /// <summary>
    /// Suite-level setup: runs once before any test in the assembly.
    /// Forces software-only (WARP) rendering for deterministic headless CI results.
    /// Also verifies that PresentationFramework is loaded from our package, not the
    /// Microsoft runtime pack.
    /// </summary>
    [OneTimeSetUp]
    public static void AssemblySuiteSetUp()
    {
        // Force software renderer — must be set before any WPF object is created.
        System.Windows.Media.RenderOptions.ProcessRenderMode =
            System.Windows.Interop.RenderMode.SoftwareOnly;

        // Hard-fail guard: the patched InitialForce.WPF DLLs are laid down in the
        // application base directory. Stock WPF resolved from the Microsoft runtime
        // pack or the shared framework (C:\Program Files\dotnet\shared\...) lives
        // outside that directory. Asserting the load location is the app base catches
        // both fallback paths, so a stock-WPF load fails loudly here instead of
        // silently invalidating the regression fixtures.
        var asm = typeof(System.Windows.Window).Assembly;
        string loc = asm.Location;
        string baseDir = System.AppContext.BaseDirectory;
        bool fromAppBase =
            !string.IsNullOrEmpty(loc) &&
            loc.StartsWith(baseDir, StringComparison.OrdinalIgnoreCase);

        if (!fromAppBase)
        {
            Assert.Fail(
                $"PresentationFramework was not loaded from the application base directory — " +
                $"the patched InitialForce.WPF DLLs did not load (likely stock/shared-framework WPF)." +
                $"\nApplication base: {baseDir}\nLoaded from:      {loc}");
        }

        TestContext.Progress.WriteLine($"[SmokeBase] PresentationFramework: {loc}");
    }

    /// <summary>
    /// Per-test setup. Subclasses may override (call base.TestSetUp() first).
    /// </summary>
    [SetUp]
    public virtual void TestSetUp()
    {
        TestContext.Progress.WriteLine($"[SmokeBase] Starting: {TestContext.CurrentContext.Test.Name}");
    }

    /// <summary>
    /// Per-test teardown. Subclasses may override.
    /// </summary>
    [TearDown]
    public virtual void TestTearDown()
    {
        TestContext.Progress.WriteLine($"[SmokeBase] Finished: {TestContext.CurrentContext.Test.Name}");
    }

    /// <summary>
    /// Runs <paramref name="action"/> on a new STA thread and rethrows any exception
    /// on the calling thread. Required for WPF objects that must be created on an STA thread.
    /// </summary>
    protected static void RunOnStaThread(Action action)
    {
        Exception? capturedException = null;
        var thread = new Thread(() =>
        {
            try { action(); }
            catch (Exception ex) { capturedException = ex; }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (capturedException is not null)
            System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(capturedException).Throw();
    }

    /// <summary>
    /// Runs <paramref name="func"/> on a new STA thread and returns its result.
    /// </summary>
    protected static T RunOnStaThread<T>(Func<T> func)
    {
        Exception? capturedException = null;
        T? result = default;
        var thread = new Thread(() =>
        {
            try { result = func(); }
            catch (Exception ex) { capturedException = ex; }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (capturedException is not null)
            System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(capturedException).Throw();
        return result!;
    }

    /// <summary>
    /// Hosts <paramref name="content"/> as the root visual of an off-screen, chrome-less
    /// <see cref="HwndSource"/> — a real <c>PresentationSource</c> — then lays it out and
    /// drives a synchronous WARP render pass over it before invoking
    /// <paramref name="probe"/>. The source is disposed in a finally block.
    ///
    /// Two things are needed and neither is satisfied by a detached Measure/Arrange:
    ///   1. Hit-testing (<c>VisualTreeHelper.HitTest</c>, <c>UIElement.InputHitTest</c>)
    ///      and item-container generation only run against a source-connected tree.
    ///   2. Hit-test geometry (drawing content) is materialized by a render pass.
    /// On a headless CI runner the offscreen window never presents frames on its own, so
    /// the render pass is forced explicitly via <see cref="RenderTargetBitmap.Render"/>
    /// (software/WARP), which materializes each visual's drawing content deterministically.
    ///
    /// Must be called on an STA thread (either a fixture marked
    /// <c>[Apartment(STA)]</c> or from inside <see cref="RunOnStaThread(Action)"/>).
    /// </summary>
    protected static void HostAndRender(FrameworkElement content, Action probe)
    {
        // Establish a client size from the content's own layout preferences.
        content.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
        Size desired = content.DesiredSize;
        int width = Math.Max(1, (int)Math.Ceiling(desired.Width));
        int height = Math.Max(1, (int)Math.Ceiling(desired.Height));

        const int wsPopup = unchecked((int)0x80000000);
        var hwndParams = new HwndSourceParameters("InitialForceSmokeHost", width, height)
        {
            WindowStyle = wsPopup,   // no chrome
            PositionX = -32000,      // park off any physical desktop
            PositionY = -32000,
        };

        var source = new HwndSource(hwndParams);
        try
        {
            source.RootVisual = content;

            // Force a full, synchronous layout at the client size so templates are
            // applied and (for ItemsControls) the container generator runs against a
            // real, constrained viewport.
            content.Measure(new Size(width, height));
            content.Arrange(new Rect(0, 0, width, height));
            content.UpdateLayout();
            DrainToRender(source.Dispatcher);
            content.UpdateLayout();

            // Force a render pass so drawing content and hit-test geometry are
            // materialized on the live visuals, independent of window presentation.
            var target = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
            target.Render(content);
            content.UpdateLayout();

            probe();
        }
        finally
        {
            source.Dispose();
        }
    }

    /// <summary>
    /// Blocks until every Dispatcher work item at <see cref="DispatcherPriority.Render"/>
    /// priority and above has run — i.e. layout and a render pass have completed. A
    /// callback queued at <see cref="DispatcherPriority.Loaded"/> (one step below
    /// Render) only runs once all higher-priority work, including the render pass,
    /// is drained, so its return is a reliable "rendered" barrier.
    /// </summary>
    protected static void DrainToRender(Dispatcher dispatcher)
    {
        dispatcher.Invoke(() => { }, DispatcherPriority.Loaded);
    }
}
