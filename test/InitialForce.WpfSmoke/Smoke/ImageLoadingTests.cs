using NUnit.Framework;
using System;
using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-014: BitmapImage decodes all common formats without exception.
/// SMOKE-015: Benchmark is in Perf/PerfHarness.cs (ImageLoadingBench.JpegDecode100).
/// </summary>
[TestFixture]
public class ImageLoadingTests : SmokeBase
{
    /// <summary>
    /// SMOKE-014: Encodes a small BGRA bitmap to JPEG, PNG, GIF, BMP, and TIFF using the
    /// WPF encoders, decodes each stream back through BitmapImage, and verifies
    /// PixelWidth > 0 with no exceptions thrown. Generating the assets in-code exercises
    /// the full encode + decode round trip for every codec and avoids brittle hardcoded
    /// base64 blobs.
    /// </summary>
    [Test]
    public void DecodeAllFormats()
    {
        BitmapSource source = CreateTestBitmap();

        var encoders = new (string name, Func<BitmapEncoder> make)[]
        {
            ("JPEG", () => new JpegBitmapEncoder()),
            ("PNG",  () => new PngBitmapEncoder()),
            ("GIF",  () => new GifBitmapEncoder()),
            ("BMP",  () => new BmpBitmapEncoder()),
            ("TIFF", () => new TiffBitmapEncoder()),
        };

        foreach (var (name, make) in encoders)
        {
            byte[] data = Encode(make(), source);

            var bitmapImage = new BitmapImage();
            bitmapImage.BeginInit();
            bitmapImage.StreamSource = new MemoryStream(data);
            bitmapImage.CacheOption = BitmapCacheOption.OnLoad;
            bitmapImage.EndInit();
            bitmapImage.Freeze();

            Assert.That(bitmapImage.PixelWidth, Is.GreaterThan(0),
                $"BitmapImage.PixelWidth == 0 for {name} format.");
            TestContext.Progress.WriteLine(
                $"SMOKE-014 {name}: {bitmapImage.PixelWidth}x{bitmapImage.PixelHeight}");
        }
    }

    private static byte[] Encode(BitmapEncoder encoder, BitmapSource source)
    {
        encoder.Frames.Add(BitmapFrame.Create(source));
        using var ms = new MemoryStream();
        encoder.Save(ms);
        return ms.ToArray();
    }

    // A 2x2 opaque red BGRA bitmap. 2x2 (rather than 1x1) keeps every codec — including
    // the block-based JPEG encoder — comfortably within its minimum dimensions.
    private static BitmapSource CreateTestBitmap()
    {
        const int width = 2;
        const int height = 2;
        const int stride = width * 4;

        var pixels = new byte[height * stride];
        for (int i = 0; i < pixels.Length; i += 4)
        {
            pixels[i + 0] = 0;    // B
            pixels[i + 1] = 0;    // G
            pixels[i + 2] = 255;  // R
            pixels[i + 3] = 255;  // A
        }

        var bitmap = BitmapSource.Create(
            width, height, 96, 96, PixelFormats.Bgra32, null, pixels, stride);
        bitmap.Freeze();
        return bitmap;
    }
}
