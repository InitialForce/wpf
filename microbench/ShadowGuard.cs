using System;
using System.Reflection;
using System.Runtime.CompilerServices;

namespace WpfMicrobenchmarks;

/// <summary>
/// Fail-closed guard that verifies BDN's inner child process has actually
/// loaded WindowsBase / PresentationCore / System.Xaml from the
/// .dotnet-shadow root (the locally-built copies microbench.py staged), not
/// from the system Microsoft.WindowsDesktop.App runtime pack.
///
/// Catches:
///   * DOTNET_ROOT env var not propagated into the inner child
///   * Shadow root rebuilt with system DLLs (e.g. .NET update reset our pack)
///   * Per-iter swap silently failed (file lock, permissions)
///
/// Without this guard, a misconfigured shadow silently measures the upstream
/// dotnet/wpf source — every iter's verdict against unmodified product code
/// — and we wouldn't know until weeks later.
///
/// Runs as a ModuleInitializer so it fires the moment Microbenchmarks.dll
/// loads, before any [GlobalSetup] runs. Skips the outer BDN runner process
/// (which does load from the publish dir for orchestration / validation).
/// The inner child is identified by the `--anonymousPipes` argv that BDN
/// passes to it.
/// </summary>
internal static class ShadowGuard
{
    [ModuleInitializer]
    internal static void Verify()
    {
        if (!IsBdnInnerChild()) return;

        var expectedRoot = Environment.GetEnvironmentVariable("WPF_AR_EXPECTED_PACK_DIR");
        if (string.IsNullOrEmpty(expectedRoot))
        {
            throw new InvalidOperationException(
                "ShadowGuard: WPF_AR_EXPECTED_PACK_DIR is not set in the BDN inner child. " +
                "microbench.py is supposed to inject this — refusing to run benchmarks against an " +
                "unverified WPF runtime, since a misconfigured shadow would silently measure the upstream " +
                "dotnet/wpf source instead of our local build.");
        }

        Check("WindowsBase",      typeof(System.Windows.Threading.Dispatcher).Assembly, expectedRoot);
        Check("PresentationCore", typeof(System.Windows.Input.MouseDevice).Assembly,    expectedRoot);
        Check("System.Xaml",      typeof(System.Xaml.XamlReader).Assembly,              expectedRoot);
    }

    private static bool IsBdnInnerChild()
    {
        var args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length; i++)
        {
            if (string.Equals(args[i], "--anonymousPipes", StringComparison.Ordinal)) return true;
        }
        return false;
    }

    private static void Check(string name, Assembly asm, string expectedRoot)
    {
        var loc = asm.Location;
        if (string.IsNullOrEmpty(loc))
        {
            throw new InvalidOperationException(
                $"ShadowGuard: {name}.Location is empty (in-memory load?). Cannot verify shadow.");
        }

        if (!loc.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                $"ShadowGuard FAILED: {name} loaded from\n  '{loc}'\nbut expected location to start with\n  '{expectedRoot}'\n" +
                "DOTNET_ROOT shadow is not in effect — measurements would be against the system runtime pack " +
                "instead of locally-built WPF DLLs. Likely causes: env var dropped between microbench.py and BDN; " +
                "shadow root regenerated with system DLLs (e.g. .NET update); per-iter swap silently failed.");
        }
    }
}
