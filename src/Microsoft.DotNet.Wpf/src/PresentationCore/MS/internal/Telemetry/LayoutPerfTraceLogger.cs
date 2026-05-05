// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.
// See the LICENSE file in the project root for more information.

using System.Diagnostics.Tracing;
using MS.Internal.Telemetry.PresentationCore;

namespace MS.Internal.PresentationCore
{
    /// <summary>
    /// Emits per-element Measure/Arrange timing events on the
    /// "Microsoft.DOTNET.WPF.PresentationCore" TraceLogging EventSource so
    /// EventPipe / dotnet-trace captures see WPF layout hotspots without
    /// requiring classic-ETW admin sessions.
    ///
    /// Events are gated by an <see cref="EventSource.IsEnabled()"/> check and
    /// a microsecond threshold so a fast or untraced layout pass costs only
    /// a Stopwatch read per element.
    /// </summary>
    internal static class LayoutPerfTraceLogger
    {
        // Element time below this threshold is dropped — the goal is to
        // surface the few outliers, not flood the stream with sub-millisecond
        // chrome elements. Set via env var so we can tune per-capture.
        private static readonly long MeasureSlowThresholdMicros =
            ParseEnv("WPF_LAYOUT_TRACE_MEASURE_US", defaultMicros: 1000);

        private static readonly long ArrangeSlowThresholdMicros =
            ParseEnv("WPF_LAYOUT_TRACE_ARRANGE_US", defaultMicros: 1000);

        private const string MeasureSlowTag = "WpfLayoutMeasureSlow";
        private const string ArrangeSlowTag = "WpfLayoutArrangeSlow";

        private static long ParseEnv(string name, long defaultMicros)
        {
            try
            {
                var v = System.Environment.GetEnvironmentVariable(name);
                if (!string.IsNullOrEmpty(v) && long.TryParse(v, out long parsed) && parsed >= 0)
                {
                    return parsed;
                }
            }
            catch
            {
                // ignore
            }
            return defaultMicros;
        }

        public static long MeasureThresholdMicros => MeasureSlowThresholdMicros;
        public static long ArrangeThresholdMicros => ArrangeSlowThresholdMicros;

        /// <summary>
        /// Returns true only when an EventPipe (or ETW) consumer is listening
        /// to the provider — so that the caller can skip the timing check
        /// entirely on hot paths.
        /// </summary>
        public static bool IsEnabled
        {
            get
            {
                EventSource logger = TraceLoggingProvider.GetProvider();
                return logger != null && logger.IsEnabled();
            }
        }

        public static void LogSlowMeasure(string elementType, long elapsedMicros, long selfMicros,
                                          double availableWidth, double availableHeight)
        {
            EventSource logger = TraceLoggingProvider.GetProvider();
            if (logger == null || !logger.IsEnabled())
            {
                return;
            }
            logger.Write(MeasureSlowTag, TelemetryEventSource.MeasuresOptions(), new ElementMeasureTiming
            {
                ElementType = elementType,
                ElapsedMicros = elapsedMicros,
                SelfMicros = selfMicros,
                AvailableWidth = availableWidth,
                AvailableHeight = availableHeight,
            });
        }

        public static void LogSlowArrange(string elementType, long elapsedMicros, long selfMicros,
                                          double finalWidth, double finalHeight)
        {
            EventSource logger = TraceLoggingProvider.GetProvider();
            if (logger == null || !logger.IsEnabled())
            {
                return;
            }
            logger.Write(ArrangeSlowTag, TelemetryEventSource.MeasuresOptions(), new ElementArrangeTiming
            {
                ElementType = elementType,
                ElapsedMicros = elapsedMicros,
                SelfMicros = selfMicros,
                FinalWidth = finalWidth,
                FinalHeight = finalHeight,
            });
        }

        // Per-frame render breakdown — emitted once per RenderMessageHandler
        // invocation when total wall time exceeds RenderFrameSlowThresholdMicros.
        // CallbacksMicros is FireInvokeOnRenderCallbacks (which transitively
        // runs Measure/Arrange for invalidated subtrees), RenderingEventMicros
        // is the public CompositionTarget.Rendering event handlers, TargetsMicros
        // is the per-ICompositionTarget render loop, CommitMicros is the
        // Channel.Commit/CommitChannel work that hands off to the UCE thread.
        private static readonly long RenderFrameSlowThresholdMicros =
            ParseEnv("WPF_RENDER_TRACE_FRAME_US", defaultMicros: 5000);

        public static long RenderFrameThresholdMicros => RenderFrameSlowThresholdMicros;

        private const string RenderFrameSlowTag = "WpfRenderFrameSlow";

        public static void LogSlowRenderFrame(
            long elapsedMicros,
            long callbacksMicros,
            long renderingEventMicros,
            long targetsMicros,
            long commitMicros,
            int tickLoopCount,
            int registeredTargets)
        {
            EventSource logger = TraceLoggingProvider.GetProvider();
            if (logger == null || !logger.IsEnabled())
            {
                return;
            }
            logger.Write(RenderFrameSlowTag, TelemetryEventSource.MeasuresOptions(), new RenderFrameTiming
            {
                ElapsedMicros = elapsedMicros,
                CallbacksMicros = callbacksMicros,
                RenderingEventMicros = renderingEventMicros,
                TargetsMicros = targetsMicros,
                CommitMicros = commitMicros,
                TickLoopCount = tickLoopCount,
                RegisteredTargets = registeredTargets,
            });
        }

        [EventData]
        private struct ElementMeasureTiming
        {
            public string ElementType { get; set; }
            public long ElapsedMicros { get; set; }
            public long SelfMicros { get; set; }
            public double AvailableWidth { get; set; }
            public double AvailableHeight { get; set; }
        }

        [EventData]
        private struct ElementArrangeTiming
        {
            public string ElementType { get; set; }
            public long ElapsedMicros { get; set; }
            public long SelfMicros { get; set; }
            public double FinalWidth { get; set; }
            public double FinalHeight { get; set; }
        }

        [EventData]
        private struct RenderFrameTiming
        {
            public long ElapsedMicros { get; set; }
            public long CallbacksMicros { get; set; }
            public long RenderingEventMicros { get; set; }
            public long TargetsMicros { get; set; }
            public long CommitMicros { get; set; }
            public int TickLoopCount { get; set; }
            public int RegisteredTargets { get; set; }
        }
    }
}
