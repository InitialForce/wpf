using System;
using System.IO;
using System.Security.Cryptography;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Pixel-diff helper for golden image comparison.
/// SHA-256 fast path + BGRA-tolerance slow path.
/// See exec-docs/40-packaging-and-tests.md §3.4 for full specification.
/// </summary>
public static class PixelDiffHelper
{
    /// <summary>
    /// Set to true by --update-goldens CLI flag (read in Program.cs).
    /// When true, tests save rendered output as new goldens instead of comparing.
    /// </summary>
    public static bool UpdateGoldensMode { get; set; }

    /// <summary>0.001 = 0.1% pixel tolerance.</summary>
    public const double TolerancePct = 0.001;

    private static readonly string GoldensDir = Path.Combine(
        AppContext.BaseDirectory, "..", "..", "..", "..", "goldens");

    /// <summary>
    /// Renders a FrameworkElement to a RenderTargetBitmap at the specified size.
    /// Forces software-only (WARP) rendering for deterministic headless CI output.
    /// </summary>
    public static BitmapSource RenderElement(FrameworkElement element, int width, int height)
    {
        // Force WARP software renderer — must be set before first WPF render.
        System.Windows.Interop.RenderOptions.ProcessRenderMode =
            System.Windows.Interop.RenderMode.SoftwareOnly;

        element.Width  = width;
        element.Height = height;
        element.Measure(new Size(width, height));
        element.Arrange(new Rect(0, 0, width, height));
        element.UpdateLayout();

        var rtb = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
        rtb.Render(element);
        rtb.Freeze();
        return rtb;
    }

    /// <summary>
    /// Saves a rendered bitmap as a golden PNG, writing a SHA-256 sidecar file.
    /// Called when UpdateGoldensMode is true.
    /// </summary>
    public static void SaveGolden(string key, BitmapSource bitmap)
    {
        var path = GoldenPath(key);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);

        byte[] pngBytes = EncodePng(bitmap);
        File.WriteAllBytes(path, pngBytes);

        string hash = ComputeHash(pngBytes);
        File.WriteAllText(path + ".sha256", hash);
    }

    /// <summary>
    /// Compares a rendered bitmap against a stored golden.
    /// Uses SHA-256 fast path first, then per-pixel BGRA tolerance slow path.
    /// </summary>
    public static (bool matches, double diffPct) CompareToGolden(
        string key, BitmapSource rendered)
    {
        var goldenPath = GoldenPath(key);
        if (!File.Exists(goldenPath))
            throw new FileNotFoundException(
                $"Golden not found: {goldenPath}. Run with --update-goldens to create it.");

        byte[] goldenBytes   = File.ReadAllBytes(goldenPath);
        byte[] renderedBytes = EncodePng(rendered);

        // Fast path: SHA-256 equality check (canonical form, metadata stripped).
        string goldenHash   = File.ReadAllText(goldenPath + ".sha256").Trim();
        string renderedHash = ComputeHash(renderedBytes);
        if (goldenHash == renderedHash) return (true, 0.0);

        // Slow path: per-pixel tolerance check.
        double diffPct = PixelDiffPct(goldenBytes, renderedBytes);
        return (diffPct <= TolerancePct, diffPct);
    }

    private static string GoldenPath(string key)
        => Path.Combine(GoldensDir, key.Replace('/', Path.DirectorySeparatorChar));

    private static byte[] EncodePng(BitmapSource src)
    {
        // Encode without metadata (date, software) for reproducible hashes.
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(src));
        using var ms = new MemoryStream();
        encoder.Save(ms);
        return ms.ToArray();
    }

    private static string ComputeHash(byte[] data)
    {
        byte[] hash = SHA256.HashData(data);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static double PixelDiffPct(byte[] goldenPng, byte[] renderedPng)
    {
        var golden   = LoadPixels(goldenPng);
        var rendered = LoadPixels(renderedPng);
        if (golden.Length != rendered.Length) return 1.0;
        int diffCount = 0;
        int totalPixels = golden.Length / 4; // BGRA
        for (int i = 0; i < golden.Length; i += 4)
        {
            if (golden[i]   != rendered[i]   ||
                golden[i+1] != rendered[i+1] ||
                golden[i+2] != rendered[i+2] ||
                golden[i+3] != rendered[i+3])
                diffCount++;
        }
        return (double)diffCount / totalPixels;
    }

    private static byte[] LoadPixels(byte[] pngBytes)
    {
        using var ms = new MemoryStream(pngBytes);
        var decoder = BitmapDecoder.Create(
            ms, BitmapCreateOptions.None, BitmapCacheOption.OnLoad);
        var frame     = decoder.Frames[0];
        var converted = new FormatConvertedBitmap(frame, PixelFormats.Bgra32, null, 0);
        int stride    = converted.PixelWidth * 4;
        byte[] pixels = new byte[stride * converted.PixelHeight];
        converted.CopyPixels(pixels, stride, 0);
        return pixels;
    }
}
