// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

//  Synopsis: Implements class Parsers for internal use of type converters
//
//            This file contains all the code that is shared between PresentationBuildTasks and PresentationCore
//            Changes to this file will likely result in a compiler update.

using System;
using System.IO;
using System.Runtime.CompilerServices;

#if PRESENTATION_CORE

using System.Windows.Media;
using System.Windows.Media.Media3D;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows;
using MS.Internal.Media;
using TypeConverterHelper = System.Windows.Markup.TypeConverterHelper;

namespace MS.Internal

#elif PBTCOMPILER

using MS.Utility;
using MS.Internal.Markup;
using TypeConverterHelper = MS.Internal.Markup.TypeConverterHelper;

namespace MS.Internal.Markup

#endif
{
    internal static partial class Parsers
    {
#if !PBTCOMPILER
        internal static object DeserializeStreamGeometry( BinaryReader reader )
        {
            StreamGeometry geometry = new StreamGeometry();
            
            using (StreamGeometryContext context = geometry.Open())
            {
                ParserStreamGeometryContext.Deserialize( reader, context, geometry ); 
            }
            geometry.Freeze();

            return geometry; 
        }
#endif

        internal static void PathMinilanguageToBinary( BinaryWriter bw, string stringValue ) 
        {
            ParserStreamGeometryContext context = new ParserStreamGeometryContext( bw ); 
#if PRESENTATION_CORE 
            FillRule fillRule = FillRule.EvenOdd ; 
#else            
            bool fillRule = false  ; 
#endif
            ParseStringToStreamGeometryContext(context, stringValue, TypeConverterHelper.InvariantEnglishUS, ref fillRule);             
            context.SetFillRule( fillRule );                                  
            
            context.MarkEOF(); 
        }

        /// <summary>
        /// Parse a PathGeometry string.
        /// The PathGeometry syntax is the same as the PathFigureCollection syntax except that it
        /// may start with a "wsp*Fwsp*(0|1)" which indicate the winding mode (F0 is EvenOdd while
        /// F1 is NonZero).
        /// </summary>

#if !PBTCOMPILER 
        internal static Geometry ParseGeometry(
            string pathString,
            IFormatProvider formatProvider)
        {
            FillRule fillRule = FillRule.EvenOdd ;             
            StreamGeometry geometry = new StreamGeometry();
            StreamGeometryContext context = geometry.Open(); 

            ParseStringToStreamGeometryContext( context, pathString, formatProvider , ref fillRule ) ;

            // Only invoke the FillRule DP setter when the parser actually changed
            // fillRule away from the default. FillRuleProperty is registered with
            // FillRule.EvenOdd as its default value (Generated/StreamGeometry.cs),
            // so a fresh StreamGeometry already reads back EvenOdd from the
            // property store with no entry allocated. The unconditional setter
            // routes through DependencyObject.SetValueInternal which boxes via
            // FillRuleBoxes (cached, free), allocates / mutates an
            // EffectiveValueEntry to record the explicit set, runs the
            // ValidateValueCallback (IsFillRuleValid) and dispatches the
            // FillRulePropertyChanged callback. ParseStringToStreamGeometryContext
            // only assigns fillRule = Nonzero when the path starts with "F1"; for
            // every M-/m-prefixed path (the GeometryParser microbench corpus and
            // the overwhelming majority of real-world XAML path strings) the
            // setter is a pure no-op semantically, so skipping it kills the
            // per-Parse property-store work + EffectiveValueEntry alloc.
            if (fillRule != FillRule.EvenOdd)
            {
                geometry.FillRule = fillRule ;
            }
            geometry.Freeze();

            return geometry;
        }
#endif
        //
        // Given a mini-language representation of a Geometry - write it to the 
        // supplied streamgeometrycontext
        // 

        private static void ParseStringToStreamGeometryContext ( 
            StreamGeometryContext context, 
            string pathString,
            IFormatProvider formatProvider, 
#if PRESENTATION_CORE            
            ref FillRule fillRule 
#else            
            ref bool fillRule 
#endif      
            )
        {
            using ( context )
            {
                // Check to ensure that there's something to parse
                if (pathString != null)
                {
                    int curIndex = 0;

                    // skip any leading space
                    while ((curIndex < pathString.Length) && Char.IsWhiteSpace(pathString, curIndex))
                    {
                        curIndex++;
                    }

                    // Is there anything to look at?
                    if (curIndex < pathString.Length)
                    {
                        // If so, we only care if the first non-WhiteSpace char encountered is 'F'
                        if (pathString[curIndex] == 'F')
                        {
                            curIndex++;

                            // Since we found 'F' the next non-WhiteSpace char must be 0 or 1 - look for it.
                            while ((curIndex < pathString.Length) && Char.IsWhiteSpace(pathString, curIndex))
                            {
                                curIndex++;
                            }

                            // If we ran out of text, this is an error, because 'F' cannot be specified without 0 or 1
                            // Also, if the next token isn't 0 or 1, this too is illegal
                            if ((curIndex == pathString.Length) ||
                                ((pathString[curIndex] != '0') &&
                                 (pathString[curIndex] != '1')))
                            {
                                throw new FormatException(SR.Parsers_IllegalToken);
                            }
                            
#if PRESENTATION_CORE
                            fillRule = pathString[curIndex] == '0' ? FillRule.EvenOdd : FillRule.Nonzero;
#else
                            fillRule = pathString[curIndex] != '0' ; 

#endif

                            // Increment curIndex to point to the next char
                            curIndex++;
                        }
                    }

                    AbbreviatedGeometryParser parser = AbbreviatedGeometryParser.Acquire();
                    try
                    {
                        parser.ParseToGeometryContext(context, pathString, curIndex);
                    }
                    finally
                    {
                        parser.ReleaseToPool();
                    }
                }
            }
        }
    }
    
     /// <summary>
    /// Parser for XAML abbreviated geometry.
    /// SVG path spec is closely followed http://www.w3.org/TR/SVG11/paths.html
    /// 3/23/2006, new parser for performance (fyuan)
    /// </summary>
    internal sealed class AbbreviatedGeometryParser
    {
        private const bool      AllowSign    = true;
        private const bool      AllowComma   = true;
        private const bool      IsFilled     = true;
        private const bool      IsClosed     = true;
        private const bool      IsStroked    = true;
        private const bool      IsSmoothJoin = true;

        // Per-thread single-slot pool. AbbreviatedGeometryParser is stateful
        // (mutable instance fields), but ParseToGeometryContext fully overwrites
        // every used field at entry, so a previously-released instance is safe
        // to hand back without an explicit reset. Pooling kills the per-call
        // ~96 B class allocation on the Geometry.Parse hot path; on the
        // GeometryParser microbench (100 paths/op), this drops the parser
        // class allocation alone by ~9.6 KB out of the current ~89.9 KB/op
        // baseline left by iter=032 (StreamGeometryCallbackContext pool) and
        // iter=033 (FrugalStructList store pool).
        [ThreadStatic]
        private static AbbreviatedGeometryParser s_pooled;

        /// <summary>
        /// Acquire a per-thread pooled parser. Returns the [ThreadStatic]
        /// slot's current instance (clearing the slot so a nested Parse on
        /// the same thread cannot see and reuse it), or allocates a fresh
        /// one when the slot is empty (first call on the thread, or while
        /// a nested parse holds the previously-pooled instance).
        /// </summary>
        internal static AbbreviatedGeometryParser Acquire()
        {
            AbbreviatedGeometryParser parser = s_pooled;
            if (parser is null)
            {
                return new AbbreviatedGeometryParser();
            }
            s_pooled = null;
            return parser;
        }

        /// <summary>
        /// Drop reference-typed fields (so the pooled instance does not pin
        /// the parsed string, the StreamGeometryContext, or the format
        /// provider alive across calls) and publish back to the
        /// [ThreadStatic] slot. Single-slot pool: if the slot is occupied
        /// (nested parse), the redundant instance is left for GC. Value-type
        /// fields are intentionally not cleared — they are unconditionally
        /// overwritten by ParseToGeometryContext at entry.
        /// </summary>
        internal void ReleaseToPool()
        {
            _pathString = null;
            _context = null;
            _formatProvider = null;
            if (s_pooled is null)
            {
                s_pooled = this;
            }
        }

        private IFormatProvider _formatProvider;
        
        private string          _pathString;        // Input string to be parsed
        private int             _pathLength;
        private int             _curIndex;          // Location to read next character from 
        private bool            _figureStarted;     // StartFigure is effective
        
        private Point           _lastStart;         // Last figure starting point
        private Point           _lastPoint;         // Last point
        private Point           _secondLastPoint;   // The point before last point
        
        private char            _token;             // Non whitespace character returned by ReadToken

        private StreamGeometryContext _context;
        
        /// <summary>
        /// Throw unexpected token exception
        /// </summary>
        private void ThrowBadToken()
        {
            throw new System.FormatException(SR.Format(SR.Parser_UnexpectedToken, _pathString, _curIndex - 1));
        }

        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private bool More()
        {
            return _curIndex < _pathLength;
        }

        // Skip white space, one comma if allowed.
        //
        // AggressiveInlining: SkipWhiteSpace is the inner-most prelude on
        // ReadToken / IsNumber / ReadBool, all of which are called from the
        // ReadNumber + do-while hot loops in ParseToGeometryContext. Forcing
        // inlining at every call site eliminates the ~3-5 ns method-call
        // frame paid on each of the ~6700 SkipWhiteSpace invocations per
        // ParseCorpus. The body is moderately sized (~80 IL bytes incl. the
        // switch) but well within the AggressiveInlining budget; the outer
        // callers (IsNumber, ReadToken) are themselves marked AggressiveInlining
        // so the inlining cascades into ReadNumber + the loop tests.
        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private bool SkipWhiteSpace(bool allowComma)
        {
            // Hoist fields to locals so the JIT proves they don't change across
            // the loop and folds away per-iteration field loads + null-checks on
            // the string indexer. _curIndex is only written back at exit.
            string s = _pathString;
            int end = _pathLength;
            int i = _curIndex;

            bool commaMet = false;

            while (i < end)
            {
                char ch = s[i];

                switch (ch)
                {
                case ' ' :
                case '\n':
                case '\r':
                case '\t': // SVG whitespace
                    break;

                case ',':
                    if (allowComma)
                    {
                        commaMet   = true;
                        allowComma = false; // one comma only
                    }
                    else
                    {
                        _curIndex = i;
                        ThrowBadToken();
                    }
                    break;

                default:
                    // Avoid calling IsWhiteSpace for ch in (' ' .. 'z']
                    if (((ch >' ') && (ch <= 'z')) || ! Char.IsWhiteSpace(ch))
                    {
                        _curIndex = i;
                        // Stash the non-WS char into _token so callers
                        // (ReadToken, IsNumber, ReadBool) can skip a redundant
                        // _pathString[_curIndex] reload + bounds-check after
                        // SkipWhiteSpace returns. _token retains its prior value
                        // when SkipWhiteSpace exits at end-of-string (default
                        // case did not fire); callers must check More() first.
                        _token = ch;
                        return commaMet;
                    }
                    break;
                }

                i++;
            }

            _curIndex = i;
            return commaMet;
        }

        /// <summary>
        /// Read the next non whitespace character
        /// </summary>
        /// <returns>True if not end of string</returns>
        // AggressiveInlining: thin wrapper over SkipWhiteSpace + More + curIndex
        // advance. Called from the outer `while (ReadToken())` loop and inlining
        // here lets the JIT see the entire prelude (SkipWhiteSpace + More) in
        // one body and fold the loop's per-token bookkeeping with the SkipWS
        // body that follows it.
        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private bool ReadToken()
        {
            SkipWhiteSpace(!AllowComma);

            // Check for end of string. SkipWhiteSpace already stashed the
            // first non-WS char into _token when it returned via the default
            // branch; just advance _curIndex to consume it.
            if (More())
            {
                _curIndex ++;
                return true;
            }
            else
            {
                return false;
            }
        }

        // AggressiveInlining: called once per ReadNumber prelude (~5000/op) and
        // once per do-while loop test in ParseToGeometryContext (~1700/op).
        // Inlining eliminates the call-frame on the per-number hot path AND
        // — combined with SkipWhiteSpace's own AggressiveInlining — collapses
        // the prelude into a tight load+compare sequence inside ReadNumber
        // and the loop tests, killing two method-call frames per ReadNumber.
        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private bool IsNumber(bool allowComma)
        {
            bool commaMet = SkipWhiteSpace(allowComma);

            if (More())
            {
                // _token was set by SkipWhiteSpace's default-branch exit when
                // it stopped on a non-WS char; reuse it instead of doing a
                // second _pathString[_curIndex] indexer-read with bounds-check.
                char t = _token;

                // Path data is digit-dominated; check the digit range first
                // via single subtract+unsigned-compare so the hot path takes
                // one branch instead of stepping through '.', '-', '+'.
                if ((uint)(t - '0') <= 9u)
                {
                    return true;
                }

                // Other valid number starts: sign, decimal point, Infinity, NaN.
                if ((t == '.') || (t == '-') || (t == '+') || (t == 'I') || (t == 'N'))
                {
                    return true;
                }
            }

            if (commaMet) // Only allowed between numbers
            {
                ThrowBadToken();
            }

            return false;
        }

//
//         /// <summary>
//         /// See if the current token matches the string s. If so, advance and
//         /// return true. Else, return false.
//         /// </summary>
//         bool TryAdvance(string s)
//         {
//             Debug.Assert(s.Length != 0);
//
//             bool match = false;
//             if (More() && _pathString[_currentIndex] == s[0])
//             {
//                 //
//                 // Don't bother reading subsequent characters, as the CLR parser will
//                 // do this for us later.
//                 //
//                 _currentIndex = Math.Min(_currentIndex + s.Length, _pathLength);
//
//                 match = true;
//             }
//
//             return match;
//         }
//

        /// <summary>
        /// Read a floating point number
        /// </summary>
        /// <returns></returns>
        private double ReadNumber(bool allowComma)
        {
            if (!IsNumber(allowComma))
            {
                ThrowBadToken();
            }

            // Hoist _pathString / _pathLength / _curIndex into locals across
            // the whole function. The integer/period/exponent walks all share
            // the same s/end/i; keeping them in registers eliminates the
            // _curIndex = i; ... if (More()) ... _pathString[_curIndex] ping-
            // pong that the prior structure forced between each sub-walk
            // (digit run -> period scan -> exponent scan -> SkipDigits inner-
            // hoist). _curIndex is only written back once, just before return.
            string s = _pathString;
            int end = _pathLength;
            int i = _curIndex;
            int start = i;

            // IsNumber already loaded _pathString[_curIndex] into _token and
            // proved we're in bounds, so `first` is the head char of the
            // number lexeme (one of '-', '+', '.', '0'..'9', 'I', 'N').
            char first = _token;
            bool simple = true;
            int intValue = 0;

            // Sign consumption. There are numbers that cannot be preceded
            // with a sign, e.g. -NaN, but it's fine to ignore that at this
            // point — double.Parse on the slow path will catch any malformed
            // lexeme with the original error semantics.
            //
            // For the unsigned-digit dominant case (the geometry corpus is
            // ~all unsigned integers), this branch is never taken: i stays
            // == start, and the I/N pre-empt below is dispatched against
            // `first` (already in a register from _token) rather than re-
            // reading _pathString[_curIndex].
            if (first == '-' || first == '+')
            {
                i++;
            }

            // Detect the head of the number body (the char immediately after
            // the optional sign). For unsigned numbers, `first` already IS
            // the head — reuse it instead of issuing another string-indexer
            // load. For signed numbers we have to read s[i].
            char head = (first == '-' || first == '+')
                ? (i < end ? s[i] : '\0')
                : first;

            // Check for Infinity / NaN — slow path: don't bother reading the
            // rest of the lexeme, the CLR's double.Parse will validate it.
            if (head == 'I')
            {
                i = Math.Min(i + 8, end); // "Infinity" has 8 characters
                simple = false;
            }
            else if (head == 'N')
            {
                i = Math.Min(i + 3, end); // "NaN" has 3 characters
                simple = false;
            }
            else
            {
                // Walk + accumulate the integer digit run in a single pass.
                // Capture the loop-terminating char into `endChar` so the
                // following period / exponent / end-of-number checks compare
                // a register instead of re-issuing a More()+_pathString[_curIndex]
                // pair. For the integer-only dominant case in the corpus,
                // endChar is the trailing whitespace and both the period and
                // exponent branches short-circuit on a single register-resident
                // compare each.
                //
                // Overflow on intValue is benign: the (i <= start + 8) gate
                // on the simple-integer return below caps the digit count at
                // 8 (positive numbers up to 99,999,999 — well inside int32),
                // and any longer run forces simple=false anyway via the
                // period/exponent branches or via the gate, both of which
                // discard intValue and re-parse via double.Parse.
                char endChar = '\0';
                while (i < end)
                {
                    char ch = s[i];
                    uint d = (uint)(ch - '0');
                    if (d > 9u)
                    {
                        endChar = ch;
                        break;
                    }
                    intValue = intValue * 10 + (int)d;
                    i++;
                }

                // Optional period, followed by more digits.
                // SkipDigits(!AllowSign) inlined: walk plain digits, no sign.
                if (endChar == '.')
                {
                    simple = false;
                    i++;
                    endChar = '\0';
                    while (i < end)
                    {
                        char c2 = s[i];
                        uint d = (uint)(c2 - '0');
                        if (d > 9u)
                        {
                            endChar = c2;
                            break;
                        }
                        i++;
                    }
                }

                // Exponent.
                // SkipDigits(AllowSign) inlined: optional sign, then digits.
                // No need to track endChar past this point — the only post-
                // exponent action is the slow-path double.Parse.
                if (endChar == 'E' || endChar == 'e')
                {
                    simple = false;
                    i++;
                    if (i < end && (s[i] == '-' || s[i] == '+'))
                    {
                        i++;
                    }
                    while (i < end)
                    {
                        if ((uint)(s[i] - '0') > 9u)
                        {
                            break;
                        }
                        i++;
                    }
                }
            }

            _curIndex = i;

            if (simple && (i <= (start + 8))) // 32-bit integer
            {
                // Sign comes from the original first char of the number token;
                // intValue accumulated the digit-run in the loop above. Apply
                // the sign as a single conditional negate.
                return (first == '-') ? -intValue : (double)intValue;
            }
            else
            {
                try
                {
#if NET
                    return double.Parse(s.AsSpan(start, i - start), provider: _formatProvider);
#else
                    return double.Parse(s.Substring(start, i - start), provider: _formatProvider);
#endif
                }
                catch (FormatException except)
                {
                    throw new System.FormatException(SR.Format(SR.Parser_UnexpectedToken, _pathString, start), except);
                }
            }
        }

        /// <summary>
        /// Read a bool: 1 or 0
        /// </summary>
        /// <returns></returns>
        private bool ReadBool()
        {
            SkipWhiteSpace(AllowComma);

            if (More())
            {
                // _token already holds the non-WS char that SkipWhiteSpace
                // stopped on; advance past it without reloading.
                _curIndex ++;

                if (_token == '0')
                {
                    return false;
                }
                else if (_token == '1')
                {
                    return true;
                }
            }

            ThrowBadToken();

            return false;
        }
        
        /// <summary>
        /// Reflect _secondLastPoint over _lastPoint to get a new point for smooth curve
        /// </summary>
        /// <returns></returns>
        private Point Reflect()
        {
            return new Point(2 * _lastPoint.X - _secondLastPoint.X,
                             2 * _lastPoint.Y - _secondLastPoint.Y);
        }

        /// <summary>
        /// Parse a PathFigureCollection string
        /// </summary>
        internal void ParseToGeometryContext(
            StreamGeometryContext context,
            string pathString,
            int startIndex)
        {
            // We really should throw an ArgumentNullException here for context and pathString.

            // From original code
            // This is only used in call to Double.Parse
            _formatProvider = System.Globalization.CultureInfo.InvariantCulture;

            _context         = context;
            _pathString      = pathString;
            _pathLength      = pathString.Length;
            _curIndex        = startIndex;

            _secondLastPoint = new Point(0, 0);
            _lastPoint       = new Point(0, 0);
            _lastStart       = new Point(0, 0);

            _figureStarted = false;

            bool  first = true;

            char last_cmd = ' ';

            while (ReadToken()) // Empty path is allowed in XAML
            {
                char cmd = _token;

                if (first)
                {
                    if ((cmd != 'M') && (cmd != 'm'))  // Path starts with M|m
                    {
                        ThrowBadToken();
                    }

                    first = false;
                }

                // `relative` is loop-invariant for the duration of the do-while
                // bodies in the L/C/Q/A cases (cmd does not change inside the
                // inner loops). Computing it once here lets the inlined
                // ReadPoint bodies below skip a per-iteration `cmd >= 'a'`
                // compare on the integer-only fast path.
                bool relative = cmd >= 'a';

                switch (cmd)
                {
                case 'm': case 'M':
                {
                    // XAML allows multiple points after M/m.
                    // Inline ReadPoint(cmd, !AllowComma) for the M start point.
                    double mx = ReadNumber(!AllowComma);
                    double my = ReadNumber(AllowComma);
                    if (relative) { mx += _lastPoint.X; my += _lastPoint.Y; }
                    _lastPoint = new Point(mx, my);

                    context.BeginFigure(_lastPoint, IsFilled, ! IsClosed);
                    _figureStarted = true;
                    _lastStart = _lastPoint;
                    last_cmd = 'M';

                    while (IsNumber(AllowComma))
                    {
                        // Inline ReadPoint(cmd, !AllowComma) for each implicit-LineTo point.
                        double ix = ReadNumber(!AllowComma);
                        double iy = ReadNumber(AllowComma);
                        if (relative) { ix += _lastPoint.X; iy += _lastPoint.Y; }
                        _lastPoint = new Point(ix, iy);

                        context.LineTo(_lastPoint, IsStroked, ! IsSmoothJoin);
                        last_cmd = 'L';
                    }
                    break;
                }

                case 'l': case 'L':
                case 'h': case 'H':
                case 'v': case 'V':
                    // Inline EnsureFigure().
                    if (!_figureStarted)
                    {
                        context.BeginFigure(_lastStart, IsFilled, ! IsClosed);
                        _figureStarted = true;
                    }

                    do
                    {
                        switch (cmd)
                        {
                        case 'l': case 'L':
                        {
                            // Inline ReadPoint(cmd, !AllowComma) — l/L share the
                            // same body modulo `relative`, so the inner switch
                            // collapses to a single case body for the whole pair.
                            double lx = ReadNumber(!AllowComma);
                            double ly = ReadNumber(AllowComma);
                            if (relative) { lx += _lastPoint.X; ly += _lastPoint.Y; }
                            _lastPoint = new Point(lx, ly);
                            break;
                        }
                        case 'h': _lastPoint.X += ReadNumber(! AllowComma); break;
                        case 'H': _lastPoint.X  = ReadNumber(! AllowComma); break;
                        case 'v': _lastPoint.Y += ReadNumber(! AllowComma); break;
                        case 'V': _lastPoint.Y  = ReadNumber(! AllowComma); break;
                        }

                        context.LineTo(_lastPoint, IsStroked, ! IsSmoothJoin);
                    }
                    while (IsNumber(AllowComma));

                    last_cmd = 'L';
                    break;

                case 'c': case 'C': // cubic Bezier
                case 's': case 'S': // smooth cublic Bezier
                    // Inline EnsureFigure().
                    if (!_figureStarted)
                    {
                        context.BeginFigure(_lastStart, IsFilled, ! IsClosed);
                        _figureStarted = true;
                    }

                    do
                    {
                        Point p;

                        if ((cmd == 's') || (cmd == 'S'))
                        {
                            if (last_cmd == 'C')
                            {
                                p = Reflect();
                            }
                            else
                            {
                                p = _lastPoint;
                            }

                            // Inline ReadPoint(cmd, !AllowComma) -> _secondLastPoint
                            double sx = ReadNumber(!AllowComma);
                            double sy = ReadNumber(AllowComma);
                            if (relative) { sx += _lastPoint.X; sy += _lastPoint.Y; }
                            _secondLastPoint = new Point(sx, sy);
                        }
                        else
                        {
                            // Inline ReadPoint(cmd, !AllowComma) -> p
                            double px = ReadNumber(!AllowComma);
                            double py = ReadNumber(AllowComma);
                            if (relative) { px += _lastPoint.X; py += _lastPoint.Y; }
                            p = new Point(px, py);

                            // Inline ReadPoint(cmd, AllowComma) -> _secondLastPoint
                            double sx = ReadNumber(AllowComma);
                            double sy = ReadNumber(AllowComma);
                            if (relative) { sx += _lastPoint.X; sy += _lastPoint.Y; }
                            _secondLastPoint = new Point(sx, sy);
                        }

                        // Inline ReadPoint(cmd, AllowComma) -> _lastPoint
                        double lpx = ReadNumber(AllowComma);
                        double lpy = ReadNumber(AllowComma);
                        if (relative) { lpx += _lastPoint.X; lpy += _lastPoint.Y; }
                        _lastPoint = new Point(lpx, lpy);

                        context.BezierTo(p, _secondLastPoint, _lastPoint, IsStroked, ! IsSmoothJoin);

                        last_cmd = 'C';
                    }
                    while (IsNumber(AllowComma));

                    break;

                case 'q': case 'Q': // quadratic Bezier
                case 't': case 'T': // smooth quadratic Bezier
                    // Inline EnsureFigure().
                    if (!_figureStarted)
                    {
                        context.BeginFigure(_lastStart, IsFilled, ! IsClosed);
                        _figureStarted = true;
                    }

                    do
                    {
                        if ((cmd == 't') || (cmd == 'T'))
                        {
                            if (last_cmd == 'Q')
                            {
                                _secondLastPoint = Reflect();
                            }
                            else
                            {
                                _secondLastPoint = _lastPoint;
                            }

                            // Inline ReadPoint(cmd, !AllowComma) -> _lastPoint
                            double tx = ReadNumber(!AllowComma);
                            double ty = ReadNumber(AllowComma);
                            if (relative) { tx += _lastPoint.X; ty += _lastPoint.Y; }
                            _lastPoint = new Point(tx, ty);
                        }
                        else
                        {
                            // Inline ReadPoint(cmd, !AllowComma) -> _secondLastPoint
                            double sx = ReadNumber(!AllowComma);
                            double sy = ReadNumber(AllowComma);
                            if (relative) { sx += _lastPoint.X; sy += _lastPoint.Y; }
                            _secondLastPoint = new Point(sx, sy);

                            // Inline ReadPoint(cmd, AllowComma) -> _lastPoint
                            double lpx = ReadNumber(AllowComma);
                            double lpy = ReadNumber(AllowComma);
                            if (relative) { lpx += _lastPoint.X; lpy += _lastPoint.Y; }
                            _lastPoint = new Point(lpx, lpy);
                        }

                        context.QuadraticBezierTo(_secondLastPoint, _lastPoint, IsStroked, ! IsSmoothJoin);

                        last_cmd = 'Q';
                    }
                    while (IsNumber(AllowComma));

                    break;

                case 'a': case 'A':
                    // Inline EnsureFigure().
                    if (!_figureStarted)
                    {
                        context.BeginFigure(_lastStart, IsFilled, ! IsClosed);
                        _figureStarted = true;
                    }

                    do
                    {
                        // A 3,4 5, 0, 0, 6,7
                        double w        = ReadNumber(! AllowComma);
                        double h        = ReadNumber(AllowComma);
                        double rotation = ReadNumber(AllowComma);
                        bool large      = ReadBool();
                        bool sweep      = ReadBool();

                        // Inline ReadPoint(cmd, AllowComma) -> _lastPoint
                        double ax = ReadNumber(AllowComma);
                        double ay = ReadNumber(AllowComma);
                        if (relative) { ax += _lastPoint.X; ay += _lastPoint.Y; }
                        _lastPoint = new Point(ax, ay);

                        context.ArcTo(
                            _lastPoint,
                            new Size(w, h),
                            rotation,
                            large,
#if PBTCOMPILER
                            sweep,
#else
                            sweep ? SweepDirection.Clockwise : SweepDirection.Counterclockwise,
#endif
                            IsStroked,
                            ! IsSmoothJoin
                            );
                    }
                    while (IsNumber(AllowComma));

                    last_cmd = 'A';
                    break;

                case 'z':
                case 'Z':
                    // Inline EnsureFigure().
                    if (!_figureStarted)
                    {
                        context.BeginFigure(_lastStart, IsFilled, ! IsClosed);
                        _figureStarted = true;
                    }
                    context.SetClosedState(IsClosed);

                    _figureStarted = false;
                    last_cmd = 'Z';

                    _lastPoint = _lastStart; // Set reference point to be first point of current figure
                    break;

                default:
                    ThrowBadToken();
                    break;
                }
            }
        }
    }
}    
