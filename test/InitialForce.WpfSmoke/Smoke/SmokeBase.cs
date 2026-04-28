using NUnit.Framework;
using System;
using System.Threading;

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
        System.Windows.Interop.RenderOptions.ProcessRenderMode =
            System.Windows.Interop.RenderMode.SoftwareOnly;

        // Hard-fail guard: ensure we loaded our DLLs, not the Microsoft runtime pack.
        var asm = typeof(System.Windows.Window).Assembly;
        string loc = asm.Location;
        bool fromRuntimePack =
            loc.Contains(@"\.dotnet\packs\", StringComparison.OrdinalIgnoreCase) ||
            loc.Contains(@"/dotnet/packs/",  StringComparison.OrdinalIgnoreCase);

        if (fromRuntimePack)
        {
            Assert.Fail(
                $"PresentationFramework loaded from Microsoft runtime pack — " +
                $"InitialForce.WPF.targets did not fire.\nLocation: {loc}");
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
}
