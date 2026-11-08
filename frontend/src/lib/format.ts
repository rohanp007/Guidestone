const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct1 = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const pct2 = new Intl.NumberFormat("en-US", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const money0 = (n: number) => usd0.format(Math.round(n) === 0 ? 0 : n);
export const money2 = (n: number) => usd2.format(n);
export const percent = (n: number) => pct1.format(n);
export const percent2 = (n: number) => pct2.format(n);
/** $1.5k style for axes. */
export const compactMoney = (n: number) =>
  Math.abs(n) >= 1000 ? `${n < 0 ? "-" : ""}$${+(Math.abs(n) / 1000).toFixed(1)}k` : `${n < 0 ? "-" : ""}$${Math.round(Math.abs(n))}`;

/** ISO date (YYYY-MM-DD) as a local date, so there is no timezone shift. */
export const parseDay = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
};
const longDay = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" });
const shortDay = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
const monthShort = new Intl.DateTimeFormat("en-US", { month: "short" });
const monthLong = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric" });
export const dateLong = (iso: string) => longDay.format(parseDay(iso));
export const dateShort = (iso: string) => shortDay.format(parseDay(iso));
export const monthLabel = (ym: string) => `${monthShort.format(parseDay(`${ym}-01`))} ’${ym.slice(2, 4)}`;
export const monthFull = (ym: string) => monthLong.format(parseDay(`${ym}-01`));
