using System.Text.Json;
using WpfPerfDiff.Models;

namespace WpfPerfDiff;

/// <summary>
/// Loads and validates one or more wpf-perf-analyze JSON output files.
/// Accepts a comma-separated list of paths or a single path.
/// </summary>
public static class AnalysisLoader
{
    private static readonly JsonSerializerOptions s_jsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
    };

    /// <summary>
    /// Parse the spec (comma-separated paths or single path) and load all runs.
    /// </summary>
    public static List<AnalysisResult> Load(string spec)
    {
        var paths = spec.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (paths.Length == 0)
            throw new ArgumentException($"No paths specified in: {spec}");

        var results = new List<AnalysisResult>(paths.Length);
        foreach (var path in paths)
            results.Add(LoadOne(path));

        return results;
    }

    private static AnalysisResult LoadOne(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException($"Analysis JSON not found: {path}", path);

        using var stream = File.OpenRead(path);
        var result = JsonSerializer.Deserialize<AnalysisResult>(stream, s_jsonOptions)
            ?? throw new InvalidDataException($"Null deserialization result for: {path}");

        Validate(result, path);
        return result;
    }

    private static void Validate(AnalysisResult r, string path)
    {
        // Warn (don't throw) on obviously empty / suspiciously short runs.
        if (r.Process == null)
            Console.Error.WriteLine($"[warn] {path}: missing 'process' section.");

        if (r.CaptureSpanMs < 100 && r.Process?.WallClockMs < 100)
            Console.Error.WriteLine($"[warn] {path}: captureSpanMs={r.CaptureSpanMs:F0} ms seems very short.");

        if (r.TopCpuMethods.Count == 0)
            Console.Error.WriteLine($"[warn] {path}: topCpuMethods is empty.");
    }
}
