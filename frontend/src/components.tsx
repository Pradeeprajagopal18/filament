import mermaid from 'mermaid'
import { useEffect, useId, useState } from 'react'

mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'strict' })

export function Mermaid({ code }: { code: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '')
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    mermaid.render(`m${id}`, code)
      .then(r => { if (!cancelled) { setSvg(r.svg); setError('') } })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [code, id])
  if (error) return <pre className="error">Diagram error: {error}</pre>
  return <div className="mermaid" dangerouslySetInnerHTML={{ __html: svg }} />
}

/** Renders artifact markdown: headings as headings, other text as-is, ```mermaid blocks as diagrams. */
export function Content({ text }: { text: string }) {
  const parts = text.split(/```mermaid\n([\s\S]*?)```/g)
  return (
    <div className="content">
      {parts.map((part, i) => i % 2 === 1 ? <Mermaid key={i} code={part} /> : <Prose key={i} text={part} />)}
    </div>
  )
}

function Prose({ text }: { text: string }) {
  // Split out markdown headings; everything else stays preformatted (lists, tables, code).
  const blocks: { h?: number; body: string }[] = []
  for (const line of text.split('\n')) {
    const m = /^(#{1,4})\s+(.*)$/.exec(line)
    if (m) blocks.push({ h: m[1].length, body: m[2] })
    else if (blocks.length && blocks[blocks.length - 1].h === undefined) blocks[blocks.length - 1].body += '\n' + line
    else blocks.push({ body: line })
  }
  return <>{blocks.map((b, i) => b.h
    ? <div key={i} className={`md-h md-h${b.h}`}>{b.body}</div>
    : b.body.trim() && <pre key={i}>{b.body.trim()}</pre>)}</>
}

export function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${value.replace(/[^a-z_]/gi, '')}`}>{value.replace('_', ' ')}</span>
}

export function Tokens({ n }: { n: number }) {
  return <>{n.toLocaleString()}</>
}

export function ErrorLine({ error }: { error: string }) {
  return error ? <p className="error">{error}</p> : null
}
