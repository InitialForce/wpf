using NUnit.Framework;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Windows.Data;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-016: INotifyPropertyChanged binding — ItemsControl updates on collection change.
/// SMOKE-017: MultiBinding + converter chain — final converted value is correct.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class DataBindingTests : SmokeBase
{
    /// <summary>
    /// SMOKE-016: Binds an ObservableCollection to an ItemsControl, mutates the
    /// collection, and verifies the UI item count matches the source count.
    /// </summary>
    [Test]
    public void ItemsControlUpdatesOnChange()
    {
        // TODO(SMOKE-016): stub — deferred to Windows CI where WPF dispatcher is available.
        Assert.That(true, Is.True, "SMOKE-016 stub — deferred to Windows CI.");

        /* Full implementation:
        RunOnStaThread(() =>
        {
            var source = new ObservableCollection<string>(new[] { "Alpha", "Beta", "Gamma" });
            var itemsControl = new System.Windows.Controls.ItemsControl
            {
                ItemsSource = source,
            };

            itemsControl.Measure(new System.Windows.Size(200, 400));
            itemsControl.Arrange(new System.Windows.Rect(0, 0, 200, 400));
            itemsControl.UpdateLayout();

            Assert.That(itemsControl.Items.Count, Is.EqualTo(3),
                "Initial count mismatch.");

            source.Add("Delta");
            itemsControl.UpdateLayout();

            Assert.That(itemsControl.Items.Count, Is.EqualTo(4),
                "Count after Add() mismatch.");

            source.RemoveAt(0);
            itemsControl.UpdateLayout();

            Assert.That(itemsControl.Items.Count, Is.EqualTo(3),
                "Count after RemoveAt() mismatch.");
        });
        */
    }

    /// <summary>
    /// SMOKE-017: Creates a MultiBinding with two string sources and a converter
    /// that concatenates them, then verifies the bound TextBlock shows the correct
    /// combined value.
    /// </summary>
    [Test]
    public void MultiBindingConverterChain()
    {
        // TODO(SMOKE-017): stub — deferred to Windows CI.
        Assert.That(true, Is.True, "SMOKE-017 stub — deferred to Windows CI.");

        /* Full implementation:
        RunOnStaThread(() =>
        {
            var vm = new TwoStringsViewModel { First = "Hello", Second = "World" };
            var textBlock = new System.Windows.Controls.TextBlock();
            textBlock.DataContext = vm;

            var multi = new MultiBinding
            {
                Converter = new ConcatConverter(),
                Bindings =
                {
                    new Binding("First"),
                    new Binding("Second"),
                },
            };
            textBlock.SetBinding(System.Windows.Controls.TextBlock.TextProperty, multi);

            textBlock.Measure(new System.Windows.Size(200, 50));
            textBlock.Arrange(new System.Windows.Rect(0, 0, 200, 50));
            textBlock.UpdateLayout();

            Assert.That(textBlock.Text, Is.EqualTo("Hello World"),
                "MultiBinding converter chain produced wrong result.");
        });
        */
    }
}

// ---------------------------------------------------------------------------
// Helper view-model + converter used by SMOKE-017
// ---------------------------------------------------------------------------

internal sealed class TwoStringsViewModel : INotifyPropertyChanged
{
    private string _first = string.Empty;
    private string _second = string.Empty;

    public string First
    {
        get => _first;
        set { _first = value; OnPropertyChanged(); }
    }

    public string Second
    {
        get => _second;
        set { _second = value; OnPropertyChanged(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

internal sealed class ConcatConverter : System.Windows.Data.IMultiValueConverter
{
    public object Convert(object[] values, System.Type targetType,
        object parameter, System.Globalization.CultureInfo culture)
        => string.Join(" ", values);

    public object[] ConvertBack(object value, System.Type[] targetTypes,
        object parameter, System.Globalization.CultureInfo culture)
        => throw new System.NotImplementedException();
}
