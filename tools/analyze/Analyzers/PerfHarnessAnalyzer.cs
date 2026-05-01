using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Parses MotionCatalyst-PerfHarness EventSource events from the ETL file.
///
/// EventSource name: "MotionCatalyst-PerfHarness"
/// Provider GUID: computed from EventSource name using the standard BCL algorithm.
///
/// Event schema (from W1.3 spec):
///   Event 1  ScenarioStart  — fields: Scenario (string)
///   Event 2  ScenarioEnd    — fields: Scenario (string)
///   Event 3  StepStart      — fields: StepName (string)
///   Event 4  StepEnd        — fields: StepName (string), ElapsedMs (double)
///   Event 5  IdleDetected   — fields: StepName (string)
///   Event 6  ScenarioSentinel — no payload
///
/// Step timings are computed by pairing StepStart/StepEnd by step name.
/// The ElapsedMs field from StepEnd overrides computed duration if present.
/// </summary>
internal sealed class PerfHarnessAnalyzer
{
    // GUID computed from "MotionCatalyst-PerfHarness" using the standard
    // EventSource name-to-GUID algorithm (BCL version 5 UUID with SHA1).
    private static readonly Guid s_perfHarnessProviderGuid =
        ComputeEventSourceGuid("MotionCatalyst-PerfHarness");

    private const int EventIdScenarioStart = 1;
    private const int EventIdScenarioEnd = 2;
    private const int EventIdStepStart = 3;
    private const int EventIdStepEnd = 4;
    private const int EventIdIdleDetected = 5;

    public static PerfHarnessMetrics? Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid,
        List<string> warnings)
    {
        if (!genericEventsPending.HasResult)
            return null;

        var events = genericEventsPending.Result.Events;

        var perfEvents = events
            .Where(e => MatchesProvider(e) && (targetPid <= 0 || e.ProcessId == targetPid))
            .OrderBy(e => e.Timestamp.TotalMilliseconds)
            .ToList();

        if (perfEvents.Count == 0)
        {
            warnings.Add("No MotionCatalyst-PerfHarness events found. " +
                         "Either W1.3 EventSource is not implemented yet or " +
                         "'MotionCatalyst-PerfHarness' provider was not enabled during capture.");
            return null;
        }

        var metrics = new PerfHarnessMetrics();
        var stepStarts = new Dictionary<string, double>(StringComparer.Ordinal);

        foreach (var evt in perfEvents)
        {
            switch (evt.Id)
            {
                case EventIdScenarioStart:
                    metrics.Scenario = GetFieldString(evt, "Scenario", "unknown");
                    metrics.ScenarioStartTimestampMs = (double)evt.Timestamp.TotalMilliseconds;
                    break;

                case EventIdScenarioEnd:
                    metrics.ScenarioEndTimestampMs = (double)evt.Timestamp.TotalMilliseconds;
                    break;

                case EventIdStepStart:
                    {
                        string stepName = GetFieldString(evt, "StepName", $"step-{evt.Id}");
                        stepStarts[stepName] = (double)evt.Timestamp.TotalMilliseconds;
                        break;
                    }

                case EventIdStepEnd:
                    {
                        string stepName = GetFieldString(evt, "StepName", $"step-{evt.Id}");
                        double endMs = (double)evt.Timestamp.TotalMilliseconds;
                        double startMs = stepStarts.TryGetValue(stepName, out double s) ? s : endMs;
                        stepStarts.Remove(stepName);

                        double payloadElapsed = GetFieldDouble(evt, "ElapsedMs", -1);
                        double elapsedMs = payloadElapsed >= 0 ? payloadElapsed : (endMs - startMs);

                        metrics.StepTimings.Add(new StepTiming
                        {
                            Name = stepName,
                            StartMs = startMs,
                            EndMs = endMs,
                            ElapsedMs = Math.Round(elapsedMs, 3),
                        });
                        break;
                    }

                case EventIdIdleDetected:
                    {
                        string stepName = GetFieldString(evt, "StepName", "?");
                        metrics.IdleDetections.Add(new IdleDetection
                        {
                            Step = stepName,
                            AtMs = (double)evt.Timestamp.TotalMilliseconds,
                        });
                        break;
                    }
            }
        }

        return metrics;
    }

    private static bool MatchesProvider(IGenericEvent evt)
    {
        if (evt.ProviderId == s_perfHarnessProviderGuid)
            return true;
        // Fallback: match by provider name if resolved (ProviderName is a plain string)
        string? providerName = evt.ProviderName;
        return providerName?.Contains("PerfHarness", StringComparison.OrdinalIgnoreCase) == true ||
               providerName?.Contains("MotionCatalyst", StringComparison.OrdinalIgnoreCase) == true;
    }

    private static string GetFieldString(IGenericEvent evt, string fieldName, string defaultValue)
    {
        try
        {
            var field = evt.Fields.FirstOrDefault(f => f.Name == fieldName);
            return field?.AsString ?? defaultValue;
        }
        catch { return defaultValue; }
    }

    private static double GetFieldDouble(IGenericEvent evt, string fieldName, double defaultValue)
    {
        try
        {
            var field = evt.Fields.FirstOrDefault(f => f.Name == fieldName);
            if (field == null) return defaultValue;
            return field.AsDouble;
        }
        catch { return defaultValue; }
    }

    /// <summary>
    /// Computes the provider GUID for a given EventSource name using the
    /// standard BCL algorithm from EventSource.cs:
    ///   1. Hash = SHA1("Provider:" + name.ToUpperInvariant() encoded as BigEndianUnicode)
    ///   2. Take first 16 bytes, set version 5 bits, construct GUID.
    /// </summary>
#pragma warning disable CA5350 // SHA1 is required by the BCL EventSource name-to-GUID algorithm
    private static Guid ComputeEventSourceGuid(string providerName)
    {
        const string prefix = "Provider:";
        string fullName = prefix + providerName.ToUpperInvariant();

        byte[] nameBytes = System.Text.Encoding.BigEndianUnicode.GetBytes(fullName);
        byte[] hash = System.Security.Cryptography.SHA1.HashData(nameBytes);

        // Construct GUID from first 16 bytes — BCL version 5 (name-based SHA-1).
        // Version nibble goes into hash[6] (high byte of Data3 field in the GUID).
        // Variant bits go into hash[8].
        hash[6] = (byte)((hash[6] & 0x0F) | 0x50);  // version 5
        hash[8] = (byte)((hash[8] & 0x3F) | 0x80);  // variant

        return new Guid(
            (uint)(hash[3] | hash[2] << 8 | hash[1] << 16 | hash[0] << 24),
            (ushort)(hash[5] | hash[4] << 8),
            (ushort)(hash[7] | hash[6] << 8),
            hash[8], hash[9], hash[10], hash[11],
            hash[12], hash[13], hash[14], hash[15]);
    }
#pragma warning restore CA5350
}
