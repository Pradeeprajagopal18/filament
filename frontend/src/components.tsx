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

/** Renders artifact markdown: text as-is, ```mermaid blocks as diagrams. */
export function Content({ text }: { text: string }) {
  const parts = text.split(/```mermaid\n([\s\S]*?)```/g)
  return (
    <div className="content">
      {parts.map((part, i) =>
        i % 2 === 1 ? <Mermaid key={i} code={part} /> : part.trim() && <pre key={i}>{part.trim()}</pre>,
      )}
    </div>
  )
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
