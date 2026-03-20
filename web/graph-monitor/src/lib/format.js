export function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }

  return new Intl.NumberFormat("en-US").format(number);
}

export function formatCompactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }

  if (number >= 1_000_000) {
    return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 0 : 1)}M`;
  }
  if (number >= 1_000) {
    return `${(number / 1_000).toFixed(number >= 10_000 ? 0 : 1)}K`;
  }

  return `${number}`;
}

export function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return "—";
  }

  if (number >= 1024 * 1024) {
    return `${(number / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (number >= 1024) {
    return `${(number / 1024).toFixed(1)} KB`;
  }

  return `${number} B`;
}

export function formatIsoTimestamp(value) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function titleCase(value) {
  if (!value) {
    return "Unknown";
  }

  return `${value}`
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function normalizeDisplayState(value) {
  if (!value) {
    return "pending";
  }

  if (value === "complete") {
    return "completed";
  }

  return value;
}

export function trimText(value, maxLength = 180) {
  if (!value) {
    return "";
  }
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}…`;
}
