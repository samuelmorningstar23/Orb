import { useEffect, useRef } from 'react'

/*──────────────────────────────────────────────────────────
  STITCH-REPLICA BACKGROUND — WebGL Aurora

  Stitch uses a raymarched SDF-masked nebula shader (via Unicorn Studio).
  We replicate the key elements with a simplified WebGL approach:
  - SDF circle mask → creates the crescent/planet-rim shape
  - FBM noise for organic texture within the glow
  - Radial gradient falloff from the rim
  - Bloom blur via CSS filter on the canvas

  Plus the existing canvas dot grid with mouse displacement.
──────────────────────────────────────────────────────────*/

// ─── DOT GRID ─────────────
const DOT_SPACING  = 24
const DOT_RADIUS   = 1
const MOUSE_RADIUS = 600
const MAX_DISPLACE = 22
const LERP_BACK    = 0.04
const NOISE_SCALE  = 0.03
const NOISE_SPEED  = 0.0006

function cheapNoise(x: number, y: number, t: number): number {
  const a = Math.sin(x * 0.8 + t) * Math.cos(y * 0.6 - t * 0.7)
  const b = Math.sin(y * 1.1 - t * 0.5) * Math.cos(x * 0.9 + t * 1.1)
  return (a + b) * 0.5
}

interface Dot { gx: number; gy: number; cx: number; cy: number }

// ─── WEBGL AURORA SHADER ─────────────
const VERT_SRC = `
  attribute vec2 a_pos;
  void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
  }
`

const FRAG_SRC = `
  precision highp float;
  uniform vec2 u_resolution;
  uniform float u_time;
  uniform float u_light_mode;

  // Simple hash for noise
  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }

  // Smooth noise
  float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
  }

  // FBM — fractional Brownian motion for organic texture
  float fbm(vec2 p) {
    float val = 0.0;
    float amp = 0.5;
    float freq = 1.0;
    for (int i = 0; i < 5; i++) {
      val += amp * noise(p * freq);
      freq *= 2.0;
      amp *= 0.5;
    }
    return val;
  }

  // SDF circle
  float sdCircle(vec2 p, vec2 center, float radius) {
    return length(p - center) - radius;
  }

  void main() {
    vec2 uv = gl_FragCoord.xy / u_resolution;
    float aspect = u_resolution.x / u_resolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);
    // ~8x faster base time — crescents complete visible sweeps in ~8-15s
    float t = u_time * 0.6;

    vec3 col = vec3(0.0);

    // ═══ ARC 1: Primary — sweeps horizontally across upper-middle ═══
    // Multi-frequency drift for non-repetitive, organic motion
    vec2 c1 = vec2(
      0.5 * aspect + sin(t * 0.17) * 0.35 * aspect + sin(t * 0.07 + 1.0) * 0.15 * aspect,
      1.85 + cos(t * 0.13) * 0.18 + sin(t * 0.09 + 2.0) * 0.08
    );
    float r1 = 1.55;
    float d1 = sdCircle(p, c1, r1);

    float rimWidth1 = 0.18;
    float glow1 = exp(-d1 * d1 / (rimWidth1 * rimWidth1));

    // FBM offset scrolls visibly along the arc
    float n1 = fbm(p * 3.0 + vec2(t * 0.3, t * 0.2));
    glow1 *= 0.6 + 0.5 * n1;

    float mask1 = smoothstep(-0.02, 0.08, d1);
    glow1 *= mask1;

    vec3 gold1 = mix(vec3(1.0, 0.62, 0.0), vec3(1.0, 0.78, 0.2), n1);
    vec3 blue1 = mix(vec3(0.48, 0.68, 0.88), vec3(0.55, 0.75, 0.95), n1);
    vec3 col1 = mix(gold1, blue1, u_light_mode);
    col += col1 * glow1 * 0.55;

    // ═══ ARC 2: Secondary — offset right, counter-phase ═══
    vec2 c2 = vec2(
      0.7 * aspect + cos(t * 0.14 + 3.0) * 0.30 * aspect + sin(t * 0.06) * 0.12 * aspect,
      1.65 + sin(t * 0.19 + 1.5) * 0.15 + cos(t * 0.08 + 0.5) * 0.06
    );
    float r2 = 1.2;
    float d2 = sdCircle(p, c2, r2);

    float rimWidth2 = 0.15;
    float glow2 = exp(-d2 * d2 / (rimWidth2 * rimWidth2));
    float n2 = fbm(p * 3.5 + vec2(-t * 0.25, t * 0.18));
    glow2 *= 0.5 + 0.4 * n2;
    float mask2 = smoothstep(-0.02, 0.06, d2);
    glow2 *= mask2;

    vec3 gold2 = mix(vec3(0.9, 0.55, 0.0), vec3(1.0, 0.7, 0.12), n2);
    vec3 blue2 = mix(vec3(0.42, 0.62, 0.85), vec3(0.52, 0.72, 0.92), n2);
    vec3 col2 = mix(gold2, blue2, u_light_mode);
    col += col2 * glow2 * 0.4;

    // ═══ ARC 3: Bottom accent — rises from bottom, counter-drift ═══
    vec2 c3 = vec2(
      0.5 * aspect + sin(t * 0.11 + 4.0) * 0.25 * aspect + cos(t * 0.05 + 1.0) * 0.10 * aspect,
      -0.55 + cos(t * 0.16 + 2.5) * 0.12 + sin(t * 0.07) * 0.05
    );
    float r3 = 0.9;
    float d3 = sdCircle(p, c3, r3);

    float rimWidth3 = 0.12;
    float glow3 = exp(-d3 * d3 / (rimWidth3 * rimWidth3));
    float n3 = fbm(p * 2.5 + vec2(t * 0.2, -t * 0.28));
    glow3 *= 0.4 + 0.35 * n3;
    float mask3 = smoothstep(-0.02, 0.05, d3);
    glow3 *= mask3;

    vec3 gold3 = mix(vec3(0.75, 0.42, 0.0), vec3(0.9, 0.58, 0.08), n3);
    vec3 blue3 = mix(vec3(0.45, 0.65, 0.88), vec3(0.55, 0.75, 0.95), n3);
    vec3 col3 = mix(gold3, blue3, u_light_mode);
    col += col3 * glow3 * 0.3;

    // ═══ ARC 4: Accent arc — sweeps from left ═══
    vec2 c4 = vec2(
      -0.1 * aspect + cos(t * 0.2 + 1.0) * 0.20 * aspect + sin(t * 0.09 + 3.0) * 0.08 * aspect,
      1.4 + sin(t * 0.15 + 1.5) * 0.12 + cos(t * 0.06 + 4.0) * 0.05
    );
    float r4 = 1.0;
    float d4 = sdCircle(p, c4, r4);

    float rimWidth4 = 0.1;
    float glow4 = exp(-d4 * d4 / (rimWidth4 * rimWidth4));
    float n4 = fbm(p * 4.0 + vec2(t * 0.35, t * 0.15));
    glow4 *= 0.35 + 0.3 * n4;
    float mask4 = smoothstep(-0.01, 0.04, d4);
    glow4 *= mask4;

    vec3 gold4 = vec3(1.0, 0.75, 0.15);
    vec3 blue4 = vec3(0.52, 0.72, 0.92);
    vec3 col4 = mix(gold4, blue4, u_light_mode);
    col += col4 * glow4 * 0.2;

    // ═══ Center darkening — keep text area readable ═══
    float centerDist = length(vec2(uv.x - 0.5, (uv.y - 0.45) * 1.3));
    float centerDark = smoothstep(0.0, 0.5, centerDist);
    col *= mix(0.3, 1.0, centerDark);

    // ═══ Light Mode: exact Sierra Blue (#7AA5C7) with organic alpha sweeps ═══
    vec3 light_color = vec3(0.28, 0.55, 0.82);
    float total_glow = glow1 * 0.65 + glow2 * 0.5 + glow3 * 0.35 + glow4 * 0.25;
    float light_glow = total_glow * mix(0.25, 1.0, centerDark);
    // Strong alpha at peak to block out the white background and show the exact color
    float light_alpha = clamp(light_glow * 1.8, 0.12, 0.95);

    // ═══ Dark Mode: rich multi-colored gold/amber auroras ═══
    vec3 dark_color = col / (1.0 + col);
    float dark_alpha = max(max(dark_color.r, dark_color.g), dark_color.b);
    dark_alpha = clamp(dark_alpha * 2.0, 0.0, 1.0);

    // ═══ Smooth theme transition LERP ═══
    vec3 final_color = mix(dark_color, light_color, u_light_mode);
    float final_alpha = mix(dark_alpha, light_alpha, u_light_mode);

    gl_FragColor = vec4(final_color * final_alpha, final_alpha);
  }

`

function createShader(gl: WebGLRenderingContext, type: number, src: string) {
  const s = gl.createShader(type)!
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    console.error('Shader error:', gl.getShaderInfoLog(s))
    gl.deleteShader(s)
    return null
  }
  return s
}

/* This background is decorative and sits under the SIGN-IN screen, so every
   capability it wants has to be optional. A ward tablet with WebGL disabled by
   policy, or any environment without matchMedia, must lose the aurora — not the
   login form. `matchMedia` is missing in jsdom, which is exactly how this class
   of crash gets caught. */
const NO_MOTION_PREF = { matches: false, addEventListener() {}, removeEventListener() {} }
function reducedMotionQuery(): Pick<MediaQueryList, 'matches' | 'addEventListener' | 'removeEventListener'> {
  if (typeof window.matchMedia !== 'function') return NO_MOTION_PREF as unknown as MediaQueryList
  return window.matchMedia('(prefers-reduced-motion: reduce)')
}

/** Canvas contexts are null on unsupported/blocked surfaces, and jsdom throws
 *  outright rather than returning null. Both mean "no background". */
function safeContext<T>(canvas: HTMLCanvasElement, kind: string, opts?: object): T | null {
  try {
    return canvas.getContext(kind, opts) as T | null
  } catch {
    return null
  }
}

function createProgram(gl: WebGLRenderingContext, vs: WebGLShader, fs: WebGLShader) {
  const p = gl.createProgram()!
  gl.attachShader(p, vs)
  gl.attachShader(p, fs)
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
    console.error('Program error:', gl.getProgramInfoLog(p))
    return null
  }
  return p
}

export default function Aurora() {
  const glCanvasRef = useRef<HTMLCanvasElement>(null)
  const dotCanvasRef = useRef<HTMLCanvasElement>(null)
  const mouseRef = useRef({ x: -9999, y: -9999 })

  const targetLightMode = useRef(0.0)
  const currentLightMode = useRef(0.0)

  // ─── WebGL Aurora ───
  useEffect(() => {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light'
    targetLightMode.current = isLight ? 1.0 : 0.0
    currentLightMode.current = targetLightMode.current

    const canvas = glCanvasRef.current!
    const gl = safeContext<WebGLRenderingContext>(canvas, 'webgl', { alpha: true, antialias: false })
    if (!gl) return // no WebGL here — the page renders without the glow

    const vs = createShader(gl, gl.VERTEX_SHADER, VERT_SRC)
    const fs = createShader(gl, gl.FRAGMENT_SHADER, FRAG_SRC)
    if (!vs || !fs) return
    const prog = createProgram(gl, vs, fs)
    if (!prog) return

    const posLoc = gl.getAttribLocation(prog, 'a_pos')
    const resLoc = gl.getUniformLocation(prog, 'u_resolution')
    const timeLoc = gl.getUniformLocation(prog, 'u_time')
    const lightModeLoc = gl.getUniformLocation(prog, 'u_light_mode')

    // Full-screen quad
    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      -1, -1, 1, -1, -1, 1,
      -1, 1, 1, -1, 1, 1,
    ]), gl.STATIC_DRAW)

    let w = 0, h = 0, raf: number
    const t0 = performance.now()

    // Honor prefers-reduced-motion: draw a single static aurora frame and stop
    // the RAF loop (redrawing only on theme change / resize) instead of the
    // continuous flow animation.
    const reduceMQ = reducedMotionQuery()
    let reduce = reduceMQ.matches

    function resize() {
      w = Math.round(window.innerWidth * 0.5)
      h = Math.round(window.innerHeight * 0.5)
      canvas.width = w
      canvas.height = h
      canvas.style.width = window.innerWidth + 'px'
      canvas.style.height = window.innerHeight + 'px'
      gl!.viewport(0, 0, w, h)
      if (reduce) draw(STATIC_T)
    }

    const STATIC_T = 0.6 // frozen phase for a pleasant static frame

    function draw(t: number) {
      gl!.useProgram(prog)
      gl!.uniform2f(resLoc, w, h)
      gl!.uniform1f(timeLoc, t)
      gl!.uniform1f(lightModeLoc, currentLightMode.current)
      gl!.bindBuffer(gl!.ARRAY_BUFFER, buf)
      gl!.enableVertexAttribArray(posLoc)
      gl!.vertexAttribPointer(posLoc, 2, gl!.FLOAT, false, 0, 0)
      gl!.drawArrays(gl!.TRIANGLES, 0, 6)
    }

    function loop() {
      const t = (performance.now() - t0) / 1000
      // Smoothly LERP theme color shift
      currentLightMode.current += (targetLightMode.current - currentLightMode.current) * 0.08
      draw(t)
      raf = requestAnimationFrame(loop)
    }

    const handleThemeChange = () => {
      const isLight = document.documentElement.getAttribute('data-theme') === 'light'
      targetLightMode.current = isLight ? 1.0 : 0.0
      if (reduce) {
        // No lerp animation under reduced motion — snap and redraw once.
        currentLightMode.current = targetLightMode.current
        draw(STATIC_T)
      }
    }
    window.addEventListener('theme-changed', handleThemeChange)

    const handleReduceChange = () => {
      reduce = reduceMQ.matches
      if (reduce) { cancelAnimationFrame(raf); draw(STATIC_T) }
      else { cancelAnimationFrame(raf); raf = requestAnimationFrame(loop) }
    }
    reduceMQ.addEventListener('change', handleReduceChange)

    window.addEventListener('resize', resize)
    resize()
    if (reduce) draw(STATIC_T)
    else raf = requestAnimationFrame(loop)

    return () => {
      window.removeEventListener('theme-changed', handleThemeChange)
      reduceMQ.removeEventListener('change', handleReduceChange)
      window.removeEventListener('resize', resize)
      cancelAnimationFrame(raf)
    }
  }, [])

  // ─── Dot grid ───
  useEffect(() => {
    const canvas = dotCanvasRef.current!
    const ctx0 = safeContext<CanvasRenderingContext2D>(canvas, '2d')
    if (!ctx0) return
    // Re-bound after the guard so the closures below see a non-null type.
    const ctx = ctx0
    let dpr = 1, dots: Dot[] = [], w = 0, h = 0, time = 0, raf: number
    const reduceMQ = reducedMotionQuery()
    let reduce = reduceMQ.matches

    function resize() {
      dpr = window.devicePixelRatio || 1
      w = window.innerWidth; h = window.innerHeight
      canvas.width = w * dpr; canvas.height = h * dpr
      canvas.style.width = w + 'px'; canvas.style.height = h + 'px'
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

      const cols = Math.ceil(w / DOT_SPACING) + 2
      const rows = Math.ceil(h / DOT_SPACING) + 2
      const ox = (w - (cols - 1) * DOT_SPACING) / 2
      const oy = (h - (rows - 1) * DOT_SPACING) / 2
      dots = []
      for (let r = 0; r < rows; r++)
        for (let c = 0; c < cols; c++) {
          const gx = ox + c * DOT_SPACING, gy = oy + r * DOT_SPACING
          dots.push({ gx, gy, cx: gx, cy: gy })
        }
      if (reduce) drawStatic()
    }

    // static: draw the resting grid once (no noise drift, no mouse repel)
    function drawStatic() {
      ctx.clearRect(0, 0, w, h)
      const isLight = document.documentElement.getAttribute('data-theme') === 'light'
      for (const dot of dots) {
        ctx.beginPath()
        ctx.arc(dot.gx, dot.gy, DOT_RADIUS, 0, Math.PI * 2)
        ctx.fillStyle = isLight ? 'rgba(122, 165, 199, 0.23)' : 'rgba(170,170,170,0.35)'
        ctx.fill()
      }
    }

    function loop() {
      time++
      ctx.clearRect(0, 0, w, h)
      const mx = mouseRef.current.x, my = mouseRef.current.y

      for (const dot of dots) {
        const nx = cheapNoise(dot.gx * NOISE_SCALE, dot.gy * NOISE_SCALE, time * NOISE_SPEED)
        const ny = cheapNoise(dot.gy * NOISE_SCALE, dot.gx * NOISE_SCALE, time * NOISE_SPEED + 100)
        let tx = dot.gx + nx * 3, ty = dot.gy + ny * 3

        const ddx = tx - mx, ddy = ty - my
        const dist = Math.sqrt(ddx * ddx + ddy * ddy)
        if (dist < MOUSE_RADIUS && dist > 0) {
          const f = Math.pow(1 - dist / MOUSE_RADIUS, 3) * MAX_DISPLACE
          tx += (ddx / dist) * f; ty += (ddy / dist) * f
        }

        dot.cx += (tx - dot.cx) * LERP_BACK
        dot.cy += (ty - dot.cy) * LERP_BACK

        let a = 0.35
        if (dist < MOUSE_RADIUS) a = 0.35 + (1 - dist / MOUSE_RADIUS) * 0.55

        ctx.beginPath()
        ctx.arc(dot.cx, dot.cy, DOT_RADIUS, 0, Math.PI * 2)
        const isLight = document.documentElement.getAttribute('data-theme') === 'light'
        ctx.fillStyle = isLight ? `rgba(122, 165, 199, ${a * 0.65})` : `rgba(170,170,170,${a})`
        ctx.fill()
      }
      raf = requestAnimationFrame(loop)
    }

    const onMM = (e: MouseEvent) => { mouseRef.current.x = e.clientX; mouseRef.current.y = e.clientY }
    const handleReduceChange = () => {
      reduce = reduceMQ.matches
      cancelAnimationFrame(raf)
      if (reduce) drawStatic()
      else raf = requestAnimationFrame(loop)
    }
    window.addEventListener('resize', resize)
    window.addEventListener('mousemove', onMM)
    reduceMQ.addEventListener('change', handleReduceChange)
    resize()
    if (reduce) drawStatic()
    else raf = requestAnimationFrame(loop)
    return () => {
      window.removeEventListener('resize', resize)
      window.removeEventListener('mousemove', onMM)
      reduceMQ.removeEventListener('change', handleReduceChange)
      cancelAnimationFrame(raf)
    }
  }, [])

  return (
    <>
      {/* Layer 0: WebGL aurora — SDF-masked volumetric glow */}
      <canvas
        ref={glCanvasRef}
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 0,
          pointerEvents: 'none',
          imageRendering: 'auto',
        }}
      />

      {/* Layer 1: Canvas dot grid with mouse displacement */}
      <canvas
        ref={dotCanvasRef}
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 1,
          pointerEvents: 'none',
        }}
      />

      </>
  )
}
