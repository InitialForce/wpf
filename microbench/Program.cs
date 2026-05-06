using BenchmarkDotNet.Running;

namespace WpfMicrobenchmarks;

public static class Program
{
    public static int Main(string[] args)
        => BenchmarkSwitcher.FromAssembly(typeof(Program).Assembly).Run(args).Any(r => !r.HasCriticalValidationErrors)
            ? 0 : 1;
}
