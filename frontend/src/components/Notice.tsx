import type { ReactNode } from 'react'

type Variant = 'info' | 'warning' | 'error'

const TITLES: Record<Variant, string> = {
  info: 'Note',
  warning: 'Warning',
  error: 'Error',
}

export function Notice({
  variant = 'info',
  title,
  children,
}: {
  variant?: Variant
  title?: string
  children: ReactNode
}) {
  return (
    <div className={`notice notice-${variant}`} role={variant === 'error' ? 'alert' : 'status'}>
      <strong className="notice-title">{title ?? TITLES[variant]}</strong>
      <div className="notice-body">{children}</div>
    </div>
  )
}
