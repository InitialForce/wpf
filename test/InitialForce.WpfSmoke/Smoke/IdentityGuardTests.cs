using NUnit.Framework;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;

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
    /// Diffs the P/Invoke surface of the patched (app-local) WPF assemblies against the
    /// stock WPF assemblies in the runner's shared framework and fails if the patched set
    /// adds any P/Invoke not present in stock.
    ///
    /// This replaces a hand-maintained allowlist, which flagged baseline WPF P/Invokes
    /// (e.g. _AdjustWindowRectEx, CombineRgn, _CreateDIBSection) as violations. A
    /// subset-against-stock check is self-maintaining: it passes as long as the patches
    /// introduce zero new native entry points, and fails only on a genuinely added one,
    /// regardless of how the baseline evolves.
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

        var patchedAssemblies = new[]
        {
            typeof(System.Windows.Window).Assembly,          // PresentationFramework
            typeof(System.Windows.Media.Visual).Assembly,    // PresentationCore
            typeof(System.Windows.DependencyObject).Assembly,// WindowsBase
            typeof(System.Xaml.XamlReader).Assembly,         // System.Xaml
        };

        // P/Invoke signatures declared by the patched, app-local DLLs, read from their PE
        // metadata (no assembly loading required — the assemblies are already loaded, but
        // reading metadata keeps the patched/stock enumeration identical).
        var patched = new HashSet<string>(StringComparer.Ordinal);
        foreach (var asm in patchedAssemblies)
        {
            string? location = asm.Location;
            if (!string.IsNullOrEmpty(location) && File.Exists(location))
                CollectPInvokes(location, patched);
        }

        Assert.That(patched, Is.Not.Empty,
            "Enumerated zero P/Invokes from the patched WPF assemblies — the diff would be " +
            "meaningless. Check assembly resolution.");

        // P/Invoke signatures declared by the stock WPF DLLs.
        string? stockDir = ResolveStockWindowsDesktopDir();
        Assert.That(stockDir, Is.Not.Null,
            "Could not locate the stock Microsoft.WindowsDesktop.App shared-framework directory " +
            "to diff the P/Invoke surface against.");

        var stock = new HashSet<string>(StringComparer.Ordinal);
        foreach (var name in new[]
                 {
                     "PresentationFramework.dll", "PresentationCore.dll",
                     "WindowsBase.dll", "System.Xaml.dll",
                 })
        {
            string dll = Path.Combine(stockDir!, name);
            if (File.Exists(dll))
                CollectPInvokes(dll, stock);
        }

        Assert.That(stock, Is.Not.Empty,
            "Enumerated zero P/Invokes from the stock WPF assemblies — the diff would be " +
            "meaningless. Check stock-framework resolution.");

        // Baseline P/Invokes that differ from the runner's installed stock shared framework
        // only because the fork's base WPF source is a different build than the runner's
        // Microsoft.WindowsDesktop.App — i.e. pre-existing native entry points, not
        // introduced by the correctness patches. DwmExtendFrameIntoClientArea is a
        // long-standing DWM import used by WindowChrome and the Appearance/backdrop code.
        var knownBaselineSkew = new HashSet<string>(StringComparer.Ordinal)
        {
            "Standard.NativeMethods::DwmExtendFrameIntoClientArea",
        };

        var added = patched.Except(stock).Except(knownBaselineSkew)
            .OrderBy(s => s, StringComparer.Ordinal).ToList();
        Assert.That(added, Is.Empty,
            "P/Invoke(s) present in the patched WPF assemblies but absent from stock WPF — " +
            "possible security regression (a new native entry point):\n" +
            string.Join("\n", added));
    }

    // Reads all P/Invoke (PinvokeImpl) method signatures from a managed DLL's PE metadata
    // and adds "<Type.FullName>::<MethodName>" keys to <paramref name="sink"/>. Uses the
    // in-box System.Reflection.Metadata reader so no assembly is loaded for execution and
    // no out-of-band package is required.
    private static void CollectPInvokes(string dllPath, HashSet<string> sink)
    {
        using var stream = File.OpenRead(dllPath);
        using var pe = new PEReader(stream);
        if (!pe.HasMetadata)
            return;

        MetadataReader mr = pe.GetMetadataReader();
        foreach (MethodDefinitionHandle handle in mr.MethodDefinitions)
        {
            MethodDefinition method = mr.GetMethodDefinition(handle);
            if ((method.Attributes & MethodAttributes.PinvokeImpl) == 0)
                continue;

            string methodName = mr.GetString(method.Name);
            string typeName = FullTypeName(mr, method.GetDeclaringType());
            sink.Add($"{typeName}::{methodName}");
        }
    }

    // Reconstructs a Type.FullName-equivalent name (namespace-qualified, '+' for nesting,
    // backtick arity preserved) from a metadata type handle.
    private static string FullTypeName(MetadataReader mr, TypeDefinitionHandle handle)
    {
        TypeDefinition type = mr.GetTypeDefinition(handle);
        string name = mr.GetString(type.Name);

        if (type.IsNested)
            return FullTypeName(mr, type.GetDeclaringType()) + "+" + name;

        string ns = mr.GetString(type.Namespace);
        return string.IsNullOrEmpty(ns) ? name : ns + "." + name;
    }

    private static string? ResolveStockWindowsDesktopDir()
    {
        // This exe is self-contained, so the running runtime is app-local and does not
        // point at a shared framework. Locate a dotnet install that ships the stock
        // Microsoft.WindowsDesktop.App shared framework instead.
        foreach (string root in CandidateDotnetRoots())
        {
            var wpfBase = new DirectoryInfo(
                Path.Combine(root, "shared", "Microsoft.WindowsDesktop.App"));
            if (!wpfBase.Exists)
                continue;

            string? versionDir = wpfBase.GetDirectories()
                .Where(d => File.Exists(Path.Combine(d.FullName, "PresentationFramework.dll")))
                .OrderByDescending(d => ParseVersion(d.Name))
                .Select(d => d.FullName)
                .FirstOrDefault();

            if (versionDir is not null)
                return versionDir;
        }

        return null;
    }

    private static IEnumerable<string> CandidateDotnetRoots()
    {
        foreach (string envVar in new[] { "DOTNET_ROOT", "DOTNET_ROOT(x86)", "DOTNET_ROOT_X64" })
        {
            string? value = Environment.GetEnvironmentVariable(envVar);
            if (!string.IsNullOrEmpty(value))
                yield return value;
        }

        foreach (string envVar in new[] { "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)" })
        {
            string? programFiles = Environment.GetEnvironmentVariable(envVar);
            if (!string.IsNullOrEmpty(programFiles))
                yield return Path.Combine(programFiles, "dotnet");
        }

        yield return @"C:\Program Files\dotnet";
    }

    private static Version ParseVersion(string dirName)
    {
        // Strip any prerelease suffix (e.g. "10.0.0-preview.1" -> "10.0.0").
        int dash = dirName.IndexOf('-');
        string numeric = dash >= 0 ? dirName[..dash] : dirName;
        return Version.TryParse(numeric, out var v) ? v : new Version(0, 0);
    }
}
