using System.Diagnostics;
using System.Windows;

namespace InitialForce.WpfHelloWorld;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        ShowAssemblyVersion();
    }

    private void ShowAssemblyVersion()
    {
        // Display the location of PresentationFramework so the msquic-pattern
        // swap can be confirmed visually during manual testing.
        var asm = typeof(System.Windows.Window).Assembly;
        var fvi = FileVersionInfo.GetVersionInfo(asm.Location);
        VersionText.Text = $"PresentationFramework {fvi.FileVersion ?? "unknown"} — {asm.Location}";
    }

    private void CloseButton_Click(object sender, RoutedEventArgs e)
    {
        Application.Current.Shutdown();
    }
}
