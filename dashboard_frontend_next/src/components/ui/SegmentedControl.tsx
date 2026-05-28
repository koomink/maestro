export function SegmentedControl({
  label,
  value,
  values,
  onChange,
}: {
  label: string;
  value: string;
  values: string[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="segmented" aria-label={label}>
      {values.map((item) => (
        <button
          key={item}
          className={item === value ? "active" : ""}
          type="button"
          onClick={() => onChange(item)}
        >
          {item}
        </button>
      ))}
    </div>
  );
}
