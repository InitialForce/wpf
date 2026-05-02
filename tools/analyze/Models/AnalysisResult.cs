using System.Text.Json.Serialization;

namespace WpfPerfAnalyze.Models;

/// <summary>Root DTO for a single ETL analysis run.</summary>
public sealed class AnalysisResult
{
    [JsonPropertyName("etlPath")]
    public string EtlPath { get; set; } = "";

    [JsonPropertyName("fileSizeBytes")]
    public long FileSizeBytes { get; set; }

    [JsonPropertyName("captureSpanMs")]
    public double CaptureSpanMs { get; set; }

    [JsonPropertyName("process")]
    public ProcessInfo? Process { get; set; }

    [JsonPropertyName("gc")]
    public GcMetrics? Gc { get; set; }

    [JsonPropertyName("jit")]
    public JitMetrics? Jit { get; set; }

    [JsonPropertyName("wpf")]
    public WpfMetrics? Wpf { get; set; }

    [JsonPropertyName("topCpuMethods")]
    public List<CpuMethodEntry> TopCpuMethods { get; set; } = [];

    [JsonPropertyName("topAllocators")]
    public List<AllocEntry> TopAllocators { get; set; } = [];

    [JsonPropertyName("perfHarnessEvents")]
    public PerfHarnessMetrics? PerfHarnessEvents { get; set; }

    [JsonPropertyName("exceptions")]
    public ExceptionMetrics? Exceptions { get; set; }

    [JsonPropertyName("analysisWarnings")]
    public List<string> AnalysisWarnings { get; set; } = [];
}

public sealed class ProcessInfo
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("pid")]
    public int Pid { get; set; }

    [JsonPropertyName("exeVersion")]
    public string ExeVersion { get; set; } = "";

    [JsonPropertyName("wallClockMs")]
    public double WallClockMs { get; set; }

    [JsonPropertyName("cpuTimeMs")]
    public double CpuTimeMs { get; set; }

    [JsonPropertyName("userCpuMs")]
    public double UserCpuMs { get; set; }

    [JsonPropertyName("kernelCpuMs")]
    public double KernelCpuMs { get; set; }
}

public sealed class GcMetrics
{
    [JsonPropertyName("totalAllocBytes")]
    public long TotalAllocBytes { get; set; }

    [JsonPropertyName("totalGcCount")]
    public int TotalGcCount { get; set; }

    [JsonPropertyName("gen0Count")]
    public int Gen0Count { get; set; }

    [JsonPropertyName("gen1Count")]
    public int Gen1Count { get; set; }

    [JsonPropertyName("gen2Count")]
    public int Gen2Count { get; set; }

    [JsonPropertyName("totalPauseTimeMs")]
    public double TotalPauseTimeMs { get; set; }

    [JsonPropertyName("maxPauseTimeMs")]
    public double MaxPauseTimeMs { get; set; }

    [JsonPropertyName("lohAllocBytes")]
    public long LohAllocBytes { get; set; }

    [JsonPropertyName("pohAllocBytes")]
    public long PohAllocBytes { get; set; }
}

public sealed class JitMetrics
{
    [JsonPropertyName("methodsJitted")]
    public int MethodsJitted { get; set; }

    [JsonPropertyName("totalJitTimeMs")]
    public double TotalJitTimeMs { get; set; }
}

public sealed class WpfMetrics
{
    [JsonPropertyName("layoutPassCount")]
    public int LayoutPassCount { get; set; }

    [JsonPropertyName("layoutPassTotalMs")]
    public double LayoutPassTotalMs { get; set; }

    [JsonPropertyName("renderPassCount")]
    public int RenderPassCount { get; set; }

    [JsonPropertyName("renderPassTotalMs")]
    public double RenderPassTotalMs { get; set; }

    [JsonPropertyName("bamlLoadCount")]
    public int BamlLoadCount { get; set; }

    [JsonPropertyName("bamlLoadTotalMs")]
    public double BamlLoadTotalMs { get; set; }

    [JsonPropertyName("layoutPassMaxMs")]
    public double LayoutPassMaxMs { get; set; }

    [JsonPropertyName("renderFrameMaxMs")]
    public double RenderFrameMaxMs { get; set; }

    [JsonPropertyName("renderFrameP95Ms")]
    public double RenderFrameP95Ms { get; set; }

    [JsonPropertyName("dispatcherOpCount")]
    public int DispatcherOpCount { get; set; }

    [JsonPropertyName("dispatcherOpTotalMs")]
    public double DispatcherOpTotalMs { get; set; }

    [JsonPropertyName("dispatcherIdleCount")]
    public int DispatcherIdleCount { get; set; }

    [JsonPropertyName("topElementsLoaded")]
    public List<ElementLoadEntry> TopElementsLoaded { get; set; } = [];

    [JsonPropertyName("instrumented")]
    public bool Instrumented { get; set; }
}

public sealed class ElementLoadEntry
{
    [JsonPropertyName("typeName")]
    public string TypeName { get; set; } = "";

    [JsonPropertyName("count")]
    public int Count { get; set; }
}

public sealed class CpuMethodEntry
{
    [JsonPropertyName("method")]
    public string Method { get; set; } = "";

    [JsonPropertyName("module")]
    public string Module { get; set; } = "";

    [JsonPropertyName("cpuMs")]
    public double CpuMs { get; set; }

    [JsonPropertyName("samples")]
    public int Samples { get; set; }
}

public sealed class AllocEntry
{
    [JsonPropertyName("method")]
    public string Method { get; set; } = "";

    [JsonPropertyName("module")]
    public string Module { get; set; } = "";

    [JsonPropertyName("allocBytes")]
    public long AllocBytes { get; set; }

    [JsonPropertyName("allocCount")]
    public int AllocCount { get; set; }
}

public sealed class PerfHarnessMetrics
{
    [JsonPropertyName("scenario")]
    public string Scenario { get; set; } = "";

    [JsonPropertyName("scenarioStartTimestampMs")]
    public double ScenarioStartTimestampMs { get; set; }

    [JsonPropertyName("scenarioEndTimestampMs")]
    public double ScenarioEndTimestampMs { get; set; }

    [JsonPropertyName("stepTimings")]
    public List<StepTiming> StepTimings { get; set; } = [];

    [JsonPropertyName("idleDetections")]
    public List<IdleDetection> IdleDetections { get; set; } = [];
}

public sealed class StepTiming
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("startMs")]
    public double StartMs { get; set; }

    [JsonPropertyName("endMs")]
    public double EndMs { get; set; }

    [JsonPropertyName("elapsedMs")]
    public double ElapsedMs { get; set; }
}

public sealed class IdleDetection
{
    [JsonPropertyName("step")]
    public string Step { get; set; } = "";

    [JsonPropertyName("atMs")]
    public double AtMs { get; set; }
}

public sealed class ExceptionMetrics
{
    [JsonPropertyName("totalCount")]
    public int TotalCount { get; set; }

    [JsonPropertyName("byType")]
    public List<ExceptionTypeEntry> ByType { get; set; } = [];
}

public sealed class ExceptionTypeEntry
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("count")]
    public int Count { get; set; }
}
