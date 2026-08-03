// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

//
//
// Description: Implementation of GeometryGroup
//

using System.Threading;
using System.Windows.Markup;
using System.Windows.Media.Composition;

namespace System.Windows.Media
{
    #region GeometryGroup
    /// <summary>
    /// GeometryGroup
    /// </summary>
    [ContentProperty("Children")]
    public sealed partial class GeometryGroup : Geometry
    {
        #region Constructors
        /// <summary>
        /// Default constructor
        /// </summary>
        public GeometryGroup()
        {
        }
        #endregion

        #region Overrides
        /// <summary>
        /// GetPathGeometryData - returns a struct which contains this Geometry represented
        /// as a path geometry's serialized format.
        /// </summary>
        internal override PathGeometryData GetPathGeometryData()
        {
            // GeometryGroup has no serialized form of its own: producing one materializes the
            // whole group into a transient PathGeometry and re-serializes every figure point
            // (via GetTransformedFigureCollection, which bakes the group Transform into the
            // points). On the per-frame hit-test-bounds precompute path that re-serialization
            // is the dominant Point[] allocation (DESKTOP-12279). The serialized form depends
            // only on this group's Freezable subtree (Children, child geometries, transforms,
            // FillRule) - never on the pen/world matrix/tolerance a bounds query passes
            // alongside the blob - so it is cached and rebuilt only after a change anywhere in
            // the subtree (see OnChanged). This mirrors StreamGeometry, which likewise returns a
            // persistent serialized blob and invalidates on change.
            CachedPathGeometryData cache = Volatile.Read(ref _cachedPathData);
            if (cache is null)
            {
                PathGeometryData data = GetAsPathGeometry().GetPathGeometryData();

                // The native bounds routine writes the computed fill bounds back into the
                // serialized blob's MIL_PATHGEOMETRY header (BoundsValid + Bounds). Prime that
                // header here, before the blob is shared, so a later query does not mutate the
                // cached array. The cached fill bounds are matrix-independent (native applies a
                // translate/scale world matrix to the cached rect and recomputes for complex
                // matrices), so priming with the identity world matrix is valid for every later
                // query. Empty data has no header to prime and GetPathBoundsAsRB requires a
                // non-empty geometry; it also absorbs WGXERR_BADNUMBER for NaN input exactly as
                // a regular query would, so the result is discarded.
                if (!data.IsEmpty())
                {
                    PathGeometry.GetPathBoundsAsRB(
                        data,
                        null,                               // no pen
                        Matrix.Identity,
                        StandardFlatteningTolerance,
                        ToleranceType.Absolute,
                        false);                             // do not skip non-fillable figures
                }

                // Capture FillRule and Matrix from the computed data, not from the live group:
                // GetAsPathGeometry bakes the group Transform into the points and leaves the
                // path's own transform identity, and an all-empty group yields the shared empty
                // blob with FillRule.EvenOdd regardless of this group's FillRule.
                cache = new CachedPathGeometryData
                {
                    FillRule = data.FillRule,
                    Matrix = data.Matrix,
                    SerializedData = data.SerializedData
                };
                Volatile.Write(ref _cachedPathData, cache);

                return data;
            }

            return new PathGeometryData
            {
                FillRule = cache.FillRule,
                Matrix = cache.Matrix,
                SerializedData = cache.SerializedData
            };
        }

        /// <summary>
        /// Implementation of <see cref="System.Windows.Freezable.OnChanged">Freezable.OnChanged</see>.
        /// Drops the cached serialized form: Freezable change propagation calls this before any
        /// user Changed handler runs, for every change that affects the serialized subtree
        /// (FillRule, group Transform and its sub-properties, Children add/remove/replace, any
        /// child geometry property, and changes inside nested groups).
        /// </summary>
        protected override void OnChanged()
        {
            _cachedPathData = null;

            base.OnChanged();
        }

        internal override void TransformPropertyChangedHook(DependencyPropertyChangedEventArgs e)
        {
            // Defensive parity with StreamGeometry; OnChanged already covers this change.
            _cachedPathData = null;
        }

        internal override PathGeometry GetAsPathGeometry()
        {
            PathGeometry pg = new PathGeometry();
            pg.AddGeometry(this);

            pg.FillRule = FillRule;

            Debug.Assert(pg.CanFreeze);

            return pg;
        }
        
        #endregion

        #region GetPathFigureCollection
        internal override PathFigureCollection GetTransformedFigureCollection(Transform transform)
        {
            // Combine the transform argument with the internal transform
            Transform combined = new MatrixTransform(GetCombinedMatrix(transform));

            PathFigureCollection result = new PathFigureCollection();
            GeometryCollection children = Children;

            if (children != null)
            {
                for (int i = 0; i < children.Count; i++)
                {
                    PathFigureCollection pathFigures = children.Internal_GetItem(i).GetTransformedFigureCollection(combined);
                    if (pathFigures != null)
                    {
                        int count = pathFigures.Count;
                        for (int j = 0; j < count; ++j)
                        {
                            result.Add(pathFigures[j]);
                        }
                    }
                }
            }

            return result;
        }
        #endregion

        #region IsEmpty

        /// <summary>
        /// Returns true if this geometry is empty
        /// </summary>
        public override bool IsEmpty()
        {
            GeometryCollection children = Children;
            if (children == null)
            {
                return true;
            }

            for (int i=0; i<children.Count; i++)
            {
                if (!((Geometry)children[i]).IsEmpty())
                {
                    return false;
                }
            }

            return true;
        }

        internal override bool IsObviouslyEmpty()
        {
            GeometryCollection children = Children;
            return (children == null) || (children.Count == 0);
        }

        #endregion IsEmpty

        /// <summary>
        /// Returns true if this geometry may have curved segments
        /// </summary>
        public override bool MayHaveCurves()
        {
            GeometryCollection children = Children;
            if (children == null)
            {
                return false;
            }

            for (int i = 0; i < children.Count; i++)
            {
                if (((Geometry)children[i]).MayHaveCurves())
                {
                    return true;
                }
            }

            return false;
        }

        #region Cached serialized data

        /// <summary>
        /// Immutable snapshot of <see cref="GetPathGeometryData"/>'s result, held behind a single
        /// reference so lazy initialization on a frozen (cross-thread readable) instance publishes
        /// atomically. Null when the cache is cold or has been invalidated by <see cref="OnChanged"/>.
        /// </summary>
        private CachedPathGeometryData _cachedPathData;

        private sealed class CachedPathGeometryData
        {
            internal FillRule FillRule;
            internal MilMatrix3x2D Matrix;
            internal byte[] SerializedData;
        }

        #endregion
}
    #endregion
}


