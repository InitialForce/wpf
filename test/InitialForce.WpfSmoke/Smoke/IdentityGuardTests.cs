using NUnit.Framework;
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Hard-fail guard tests. These must pass on every run — they are not stubbed.
/// They verify the package injection mechanism works correctly.
///
/// Note: On Windows CI with the package present these tests run as full assertions.
/// In WSL/Linux environments without WPF, the WPF type lookups will throw
/// TypeLoadException, so the tests are skipped via Assume.That.
/// </summary>
[TestFixture]
public class IdentityGuardTests : SmokeBase
{
    /// <summary>
    /// Asserts that System.Windows.Window is loaded from our package's DLL,
    /// not from the Microsoft runtime pack.
    /// Fails if InitialForce.WPF.targets did not fire correctly.
    /// </summary>
    [Test]
    public void PresentationFrameworkIsOurVersion()
    {
        Type? windowType;
        try
        {
            windowType = Type.GetType(
                "System.Windows.Window, PresentationFramework",
                throwOnError: false);
        }
        catch
        {
            windowType = null;
        }

        // Skip on non-Windows / no-WPF environments.
        Assume.That(windowType, Is.Not.Null,
            "PresentationFramework not available in this environment — skip on non-Windows.");

        var asm = windowType!.Assembly;
        string loc = asm.Location;

        bool fromRuntimePack =
            loc.Contains(@"\.dotnet\packs\", StringComparison.OrdinalIgnoreCase) ||
            loc.Contains(@"/dotnet/packs/",  StringComparison.OrdinalIgnoreCase);

        Assert.That(fromRuntimePack, Is.False,
            $"PresentationFramework loaded from runtime pack, not our package: {loc}\n" +
            "Check that InitialForce.WPF.targets fired correctly (InjectIfWpfAssemblies target).");

        var fv = System.Diagnostics.FileVersionInfo.GetVersionInfo(loc);
        TestContext.WriteLine($"PresentationFramework.dll location : {loc}");
        TestContext.WriteLine($"PresentationFramework.dll file ver : {fv.FileVersion}");
    }

    /// <summary>
    /// Scans all loaded WPF assemblies for any [DllImport] not in the allowlist.
    /// Fails if a new P/Invoke was introduced without going through the 2x review gate.
    /// </summary>
    [Test]
    public void NoPInvokeAddedOutsideAllowlist()
    {
        // Load WPF assemblies if available; skip otherwise.
        Type? windowType;
        try
        {
            windowType = Type.GetType(
                "System.Windows.Window, PresentationFramework",
                throwOnError: false);
        }
        catch { windowType = null; }

        Assume.That(windowType, Is.Not.Null,
            "PresentationFramework not available — skip on non-Windows.");

        var allowedPInvokeNames = new HashSet<string>(StringComparer.Ordinal)
        {
            "EnableWindowWrapper", "GetWindowLongWrapper", "MapWindowPointsWrapper",
            "SetWindowLongWrapper", "SetWindowPosWrapper",
            "MilCreateResetableWaitableTimer", "WICCreateImagingFactory_Proxy",
            // Add further names here after security review only.
        };

        var ourAssemblies = new[]
        {
            typeof(System.Windows.Window).Assembly,
            typeof(System.Windows.Media.Visual).Assembly,
            typeof(System.Windows.DependencyObject).Assembly,
            typeof(System.Xaml.XamlReader).Assembly,
        };

        var violations = new List<string>();
        foreach (var asm in ourAssemblies)
        {
            foreach (var type in asm.GetTypes())
            {
                foreach (var method in type.GetMethods(
                    BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public))
                {
                    if (method.GetCustomAttributes(typeof(DllImportAttribute), false).Length > 0
                        && !allowedPInvokeNames.Contains(method.Name))
                        violations.Add($"{asm.GetName().Name}::{type.FullName}::{method.Name}");
                }
            }
        }

        Assert.That(violations, Is.Empty,
            "New [DllImport] outside allowlist — possible security regression:\n" +
            string.Join("\n", violations));
    }
}
