using System;
using System.Text;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Generates synthetic SVG path strings for SMOKE-001 (geometry parser round-trip).
/// Uses a seeded RNG so the same paths are produced on every run.
/// </summary>
internal static class SvgPathGenerator
{
    private static readonly char[] Commands = { 'M', 'L', 'H', 'V', 'C', 'Q', 'Z' };

    /// <summary>
    /// Generates <paramref name="count"/> pseudo-random SVG path strings
    /// suitable for passing to <c>System.Windows.Media.Geometry.Parse()</c>.
    /// </summary>
    public static string[] Generate(int count, int seed)
    {
        var rng    = new Random(seed);
        var result = new string[count];

        for (int i = 0; i < count; i++)
        {
            result[i] = GeneratePath(rng);
        }

        return result;
    }

    private static string GeneratePath(Random rng)
    {
        var sb = new StringBuilder();

        // Every path starts with a MoveTo.
        sb.Append('M');
        sb.Append(rng.NextDouble() * 400);
        sb.Append(',');
        sb.Append(rng.NextDouble() * 300);

        int segmentCount = rng.Next(2, 8);
        for (int s = 0; s < segmentCount; s++)
        {
            char cmd = Commands[rng.Next(0, Commands.Length - 1)]; // exclude Z except at end
            sb.Append(' ');
            sb.Append(cmd);
            switch (cmd)
            {
                case 'L':
                    sb.AppendFormat(" {0:F2},{1:F2}",
                        rng.NextDouble() * 400, rng.NextDouble() * 300);
                    break;
                case 'H':
                    sb.AppendFormat(" {0:F2}", rng.NextDouble() * 400);
                    break;
                case 'V':
                    sb.AppendFormat(" {0:F2}", rng.NextDouble() * 300);
                    break;
                case 'C':
                    // Cubic bezier: 3 control points.
                    for (int p = 0; p < 3; p++)
                        sb.AppendFormat(" {0:F2},{1:F2}",
                            rng.NextDouble() * 400, rng.NextDouble() * 300);
                    break;
                case 'Q':
                    // Quadratic bezier: 2 control points.
                    for (int p = 0; p < 2; p++)
                        sb.AppendFormat(" {0:F2},{1:F2}",
                            rng.NextDouble() * 400, rng.NextDouble() * 300);
                    break;
                case 'M':
                    sb.AppendFormat(" {0:F2},{1:F2}",
                        rng.NextDouble() * 400, rng.NextDouble() * 300);
                    break;
            }
        }

        // Close 30% of paths.
        if (rng.NextDouble() < 0.3)
            sb.Append(" Z");

        return sb.ToString();
    }
}
