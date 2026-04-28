using System;
using System.Windows;

namespace InitialForce.WpfHelloWorld;

/// <summary>
/// Explicit entry point so the startup object can be set in the csproj and
/// the app can be invoked from the msquic-pattern verification script without
/// a GUI display server (it will fail fast in that case, which is acceptable
/// since the build-output verification is purely static).
/// </summary>
[System.STAThread]
public static class Program
{
    [System.STAThread]
    public static void Main(string[] args)
    {
        var app = new App();
        app.InitializeComponent();
        app.Run();
    }
}
