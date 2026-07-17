using NUnit.Framework;
using System.Linq;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-008: VirtualizingStackPanel container recycling.
/// Verifies that binding 100,000 items realizes only ~30 containers (virtualization working).
/// </summary>
[TestFixture]
[Apartment(System.Threading.ApartmentState.STA)]
public class VirtualizingPanelTests : SmokeBase
{
    /// <summary>
    /// SMOKE-008: Binds 100,000 items to a ListBox with VirtualizingStackPanel and
    /// verifies that ItemContainerGenerator realizes only 30 ± 10 containers
    /// (i.e. virtualization is active and container recycling works correctly).
    /// </summary>
    [Test]
    public void Only30ContainersRealized()
    {
        RunOnStaThread(() =>
        {
            var items = Enumerable.Range(0, 100_000).Select(i => $"Item {i}").ToList();
            var listBox = new System.Windows.Controls.ListBox
            {
                Width = 400,
                Height = 600,
                ItemsSource = items,
            };
            System.Windows.Controls.VirtualizingStackPanel.SetIsVirtualizing(listBox, true);
            System.Windows.Controls.VirtualizingStackPanel.SetVirtualizationMode(
                listBox, System.Windows.Controls.VirtualizationMode.Recycling);

            int realized = 0;

            // Virtualization only runs once the ListBox is hosted in a source-connected,
            // rendered tree with a constrained viewport; a detached UpdateLayout realizes
            // nothing (or everything). Host it in a live window and render.
            HostAndRender(listBox, () =>
            {
                // ItemContainerGenerator.Items is the full item list (100k). The realized
                // container count is the number of indices with a materialized container.
                var generator = listBox.ItemContainerGenerator;
                for (int i = 0; i < items.Count; i++)
                {
                    if (generator.ContainerFromIndex(i) != null)
                        realized++;
                }
            });

            Assert.That(realized, Is.InRange(20, 40),
                $"Expected ~30 realized containers for 100k-item list; got {realized}. " +
                "Possible virtualization regression.");
        });
    }
}
