interface PlaceholderPageProps {
  title: string;
}

export default function PlaceholderPage({ title }: PlaceholderPageProps) {
  return (
    <div className="flex flex-col items-center justify-center h-64 text-slate-400">
      <div className="text-4xl mb-4">🚧</div>
      <h2 className="text-xl font-semibold text-slate-600">{title}</h2>
      <p className="text-sm mt-2">This section is available in the full implementation.</p>
    </div>
  )
}
