using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Cpu;
using Microsoft.Windows.EventTracing.Processes;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Extracts per-process CPU time (user + kernel) and wall-clock duration
/// from CPU scheduling data and process lifetime events.
/// </summary>
internal sealed class ProcessAnalyzer
{
    private readonly string _processName;

    public ProcessAnalyzer(string processName)
    {
        _processName = processName;
    }

    public ProcessInfo? Analyze(
        IPendingResult<IProcessDataSource> processesPending,
        IPendingResult<ICpuSchedulingDataSource> cpuSchedulingPending,
        List<string> warnings)
    {
        IProcessDataSource? processData = processesPending.HasResult ? processesPending.Result : null;
        ICpuSchedulingDataSource? cpuSched = cpuSchedulingPending.HasResult ? cpuSchedulingPending.Result : null;

        if (processData == null)
        {
            warnings.Add("ProcessDataSource not available — no process info.");
            return null;
        }

        var target = FindTargetProcess(processData, warnings);
        if (target == null)
            return null;

        double wallClockMs = 0;
        if (target.Duration.HasValue)
            wallClockMs = (double)target.Duration.Value.TotalMilliseconds;

        string exeVersion = "";
        if (target.Images != null)
        {
            // Try to find main exe image to get its version
            var exeImage = target.Images.FirstOrDefault(
                img => img.FileName != null &&
                       img.FileName.Contains(
                           Path.GetFileNameWithoutExtension(_processName),
                           StringComparison.OrdinalIgnoreCase));
            if (exeImage?.FileVersion != null)
                exeVersion = exeImage.FileVersion;
        }

        var result = new ProcessInfo
        {
            Name = target.ImageName ?? _processName,
            Pid = target.Id,
            ExeVersion = exeVersion,
            WallClockMs = wallClockMs,
        };

        if (cpuSched != null)
            ComputeCpuTime(cpuSched, target.Id, result, warnings);
        else
            warnings.Add("CpuSchedulingDataSource not available — CPU time will be 0.");

        return result;
    }

    private IProcess? FindTargetProcess(IProcessDataSource processData, List<string> warnings)
    {
        IProcess? target = null;

        foreach (var proc in processData.Processes)
        {
            string imageName = proc.ImageName ?? "";
            if (imageName.Contains(_processName, StringComparison.OrdinalIgnoreCase))
            {
                // Prefer the process with the longest duration (skip brief sub-processes)
                if (target == null)
                {
                    target = proc;
                }
                else if (proc.Duration.HasValue && target.Duration.HasValue &&
                         proc.Duration.Value.TotalMilliseconds > target.Duration.Value.TotalMilliseconds)
                {
                    target = proc;
                }
            }
        }

        if (target == null)
        {
            warnings.Add($"Process '{_processName}' not found in trace. " +
                         $"Available: {string.Join(", ", processData.Processes.Select(p => p.ImageName ?? "?").Distinct().Take(10))}");
        }

        return target;
    }

    private static void ComputeCpuTime(
        ICpuSchedulingDataSource cpuSched,
        int targetPid,
        ProcessInfo result,
        List<string> warnings)
    {
        double totalMs = 0;
        double userMs = 0;
        double kernelMs = 0;

        foreach (var slice in cpuSched.CpuTimeSlices)
        {
            if (slice.Process?.Id != targetPid)
                continue;

            double sliceMs = (double)slice.Duration.TotalMilliseconds;
            totalMs += sliceMs;

            // IsUserMode on switch-out tells whether the thread was in user mode
            bool isUser = slice.SwitchOut?.IsUserMode ?? true;
            if (isUser)
                userMs += sliceMs;
            else
                kernelMs += sliceMs;
        }

        result.CpuTimeMs = totalMs;
        result.UserCpuMs = userMs;
        result.KernelCpuMs = kernelMs;

        if (totalMs == 0)
            warnings.Add("CPU scheduling data produced 0ms for target process — capture may lack kernel events.");
    }
}
