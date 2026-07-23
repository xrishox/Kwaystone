/** Currency Exchange loop analysis for the Alt+S / Alt+A workflow. */
import { exchangePairSnapshot, exchangeSnapshotEpoch } from "./poe2scout";
import {
  SCOUT_MAX_AGE_MS,
  ScoutConfidence,
  scoutConfidence,
} from "./scout/policy";

const DEFAULT_CAPTURE_MAX_AGE_MS = 120_000;
const DEFAULT_MIN_PERCENT = 5;
const DEFAULT_SAFETY_BUFFER_BPS = 500;
const DEFAULT_EXECUTION_CONCESSION_BPS = 500;
const MAX_SAFETY_BUFFER_BPS = 1_500;
const MAX_EXECUTION_CONCESSION_BPS = 1_500;
const QUANTITY_MAX = 100;

export type ExchangeItem = {
  apiId: string;
  name: string;
  category: string;
  iconUrl?: string;
  isCurrency?: boolean;
};

export type PairObservation = {
  id: string;
  want: ExchangeItem;
  have: ExchangeItem;
  wantAmount: number;
  haveAmount: number;
  rate: number;
  observedAt: number;
};

export type LoopLeg = {
  from: ExchangeItem;
  to: ExchangeItem;
  rate: number;
  executionRate?: number;
  source:
    | "capture"
    | "capture-bridge"
    | "poe2scout";
  observedAt?: number;
  inputAmount?: number;
  outputAmount?: number;
  scoutEvidence?: {
    fromVolume: number;
    toVolume: number;
    liquidityExalted: number;
    confidence: ScoutConfidence;
  };
};

export type ArbLoop = {
  id: string;
  path: ExchangeItem[];
  legs: LoopLeg[];
  multiplier: number;
  percent: number;
  nominalMultiplier: number;
  nominalPercent: number;
  executionMultiplier: number;
  executionPercent: number;
  bufferedMultiplier: number;
  bufferedPercent: number;
  status: "verified" | "estimate";
  estimateConfidence?: ScoutConfidence;
  validUntil?: number;
  stale: boolean;
  actionable: boolean;
  quantityOutcomes: QuantityOutcome[];
};

export type QuantityStepOutcome = {
  nominalInputUnits: number;
  nominalOutputUnits: number;
  nominalExecuted: boolean;
  executionInputUnits: number;
  executionOutputUnits: number;
  executionExecuted: boolean;
  bufferedInputUnits: number;
  bufferedOutputUnits: number;
  bufferedExecuted: boolean;
  boundaryHeadroomPercent: number;
};

export type QuantityOutcome = {
  quantity: number;
  nominalFinalUnits: number;
  bufferedFinalUnits: number;
  nominalComplete: boolean;
  executionFinalUnits: number;
  executionComplete: boolean;
  bufferedComplete: boolean;
  nominalBlockedStep?: number;
  executionBlockedStep?: number;
  bufferedBlockedStep?: number;
  nominalBlockedUnits?: number;
  executionBlockedUnits?: number;
  bufferedBlockedUnits?: number;
  nominalReturnPercent: number | null;
  executionReturnPercent: number | null;
  bufferedReturnPercent: number | null;
  steps: QuantityStepOutcome[];
  localScore: number;
  localPeak: boolean;
  budgetBest: boolean;
  actionable: boolean;
};

export type VerificationNeed = {
  from: ExchangeItem;
  to: ExchangeItem;
  hotkey: "Alt+A";
  reason: "poe2scout";
};

export type CaptureView = PairObservation & {
  role: "buy" | "sell";
  quote: ExchangeItem;
  stale: boolean;
  validUntil: number;
};

export type BridgeCaptureView = PairObservation & {
  stale: boolean;
  validUntil: number;
};

export type ArbAnalysis = {
  target: ExchangeItem;
  captures: CaptureView[];
  bridges: BridgeCaptureView[];
  loops: ArbLoop[];
  bestVerifiedLoop?: ArbLoop;
  bestCandidateLoop?: ArbLoop;
  loopsEvaluated: number;
  capturedCurrencyCount: number;
  unavailable: string[];
  verificationNeeded: VerificationNeed[];
  ratesEpoch?: string;
  ratesSnapshotId?: number;
  ratesFetchedAt: number;
  ratesAgeMs: number;
  ratesStatus: "fresh" | "stale" | "degraded";
  safetyBufferBps: number;
  perLegSafetyBufferBps: number;
  executionConcessionBps: number;
  executionConcessionLoopPercent: number;
  analyzedAt: number;
};

type Rational = { numerator: bigint; denominator: bigint };

type PairSide = {
  ApiId?: string;
  Text?: string;
  CategoryApiId?: string;
  IconUrl?: string;
  ItemMetadata?: { icon?: string; name?: string };
};

type SideData = {
  VolumeTraded?: number;
};

type SnapshotPair = {
  CurrencyExchangeSnapshotId?: number;
  Volume?: number;
  CurrencyOne?: PairSide;
  CurrencyTwo?: PairSide;
  CurrencyOneData?: SideData;
  CurrencyTwoData?: SideData;
};

type RateBook = {
  league: string;
  epoch?: string;
  fetchedAt: number;
  catalog: Map<string, ExchangeItem>;
  snapshotId?: number;
  rates: Map<string, {
    rate: number;
    liquidity: number;
    fromVolume: number;
    toVolume: number;
    confidence: ScoutConfidence;
  }>;
  degraded: boolean;
};

const cachedBooks = new Map<string, RateBook>();

function pairsOf(raw: unknown): SnapshotPair[] {
  return Array.isArray(raw) ? (raw as SnapshotPair[]) : [];
}

function normName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

export function isCurrencyCategory(value: unknown): boolean {
  return typeof value === "string" && value.trim().toLowerCase() === "currency";
}

function pairKey(from: string, to: string): string {
  return `${from}->${to}`;
}

function gcd(left: bigint, right: bigint): bigint {
  let a = left < 0n ? -left : left;
  let b = right < 0n ? -right : right;
  while (b) [a, b] = [b, a % b];
  return a || 1n;
}

function rational(numerator: bigint, denominator: bigint): Rational {
  if (denominator === 0n) throw new Error("zero rational denominator");
  const sign = denominator < 0n ? -1n : 1n;
  const divisor = gcd(numerator, denominator);
  return {
    numerator: sign * numerator / divisor,
    denominator: sign * denominator / divisor,
  };
}

function rationalFromString(raw: string): Rational | null {
  const match = raw.trim().match(/^([+]?[0-9]+)(?:\.([0-9]*))?(?:e([+-]?[0-9]+))?$/i);
  if (!match) return null;
  const whole = match[1].replace("+", "");
  const fraction = match[2] ?? "";
  const exponent = Number(match[3] ?? 0);
  if (!Number.isInteger(exponent)) return null;
  let numerator = BigInt(`${whole}${fraction}` || "0");
  const scale = fraction.length - exponent;
  let denominator = 1n;
  if (scale > 0) denominator = 10n ** BigInt(scale);
  else if (scale < 0) numerator *= 10n ** BigInt(-scale);
  return rational(numerator, denominator);
}

function rationalFromNumber(value: unknown): Rational | null {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  return rationalFromString(String(number));
}

function multiply(left: Rational, right: Rational): Rational {
  return rational(
    left.numerator * right.numerator,
    left.denominator * right.denominator,
  );
}

function divide(left: Rational, right: Rational): Rational {
  return rational(
    left.numerator * right.denominator,
    left.denominator * right.numerator,
  );
}

function rationalNumber(value: Rational): number {
  return Number(value.numerator) / Number(value.denominator);
}

function floorProduct(units: number, rate: Rational): number {
  return Number(BigInt(units) * rate.numerator / rate.denominator);
}

function displayedRate(rate: number): Rational | null {
  if (!Number.isFinite(rate) || rate <= 0) return null;
  const larger = rate >= 1 ? rate : 1 / rate;
  const displayed = rationalFromString(larger.toFixed(2));
  if (!displayed) return null;
  return rate >= 1 ? displayed : rational(displayed.denominator, displayed.numerator);
}

function canonicalLegRate(leg: LoopLeg): Rational | null {
  const input = rationalFromNumber(leg.inputAmount);
  const output = rationalFromNumber(leg.outputAmount);
  if (input && output) return divide(output, input);
  return displayedRate(leg.rate);
}

function safetyBufferBps(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DEFAULT_SAFETY_BUFFER_BPS;
  const clamped = Math.max(0, Math.min(MAX_SAFETY_BUFFER_BPS, parsed));
  return Math.round(clamped / 50) * 50;
}

function executionConcessionBps(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DEFAULT_EXECUTION_CONCESSION_BPS;
  const clamped = Math.max(0, Math.min(MAX_EXECUTION_CONCESSION_BPS, parsed));
  return Math.round(clamped / 50) * 50;
}

function perLegStress(bufferBps: number, legCount: number): Rational {
  if (bufferBps <= 0 || legCount <= 0) return rational(1n, 1n);
  const totalFactor = (10_000 - bufferBps) / 10_000;
  const scale = 1_000_000_000_000;
  // Round toward a worse rate so floating-point conversion can never make the
  // configured total loop buffer less conservative at an integer boundary.
  const scaled = Math.floor(totalFactor ** (1 / legCount) * scale);
  return rational(BigInt(scaled), BigInt(scale));
}

type UnitSimulation = {
  finalUnits: number;
  complete: boolean;
  blockedStep?: number;
  blockedUnits?: number;
  inputs: number[];
  outputs: number[];
  executed: boolean[];
};

function simulateQuantity(quantity: number, rates: Rational[]): UnitSimulation {
  let current = quantity;
  let complete = true;
  let blockedStep: number | undefined;
  let blockedUnits: number | undefined;
  const inputs: number[] = [];
  const outputs: number[] = [];
  const executed: boolean[] = [];
  for (const [stepIndex, rate] of rates.entries()) {
    const input = complete ? current : 0;
    const output = complete ? floorProduct(input, rate) : 0;
    const didExecute = complete && output > 0;
    inputs.push(input);
    outputs.push(output);
    executed.push(didExecute);
    if (complete && !didExecute) {
      complete = false;
      blockedStep = stepIndex;
      blockedUnits = input;
    } else if (complete) {
      current = output;
    }
  }
  return {
    finalUnits: complete ? current : 0,
    complete,
    ...(blockedStep !== undefined ? { blockedStep } : {}),
    ...(blockedUnits !== undefined ? { blockedUnits } : {}),
    inputs,
    outputs,
    executed,
  };
}

function quantityOutcomes(
  marketRates: Rational[],
  concessionBps: number,
  bufferBps: number,
  status: ArbLoop["status"],
  stale: boolean,
  minPercent: number,
): QuantityOutcome[] {
  const concession = rational(BigInt(10_000 - concessionBps), 10_000n);
  const stress = perLegStress(bufferBps, marketRates.length);
  const executionRates = marketRates.map((rate) => multiply(rate, concession));
  const bufferedRates = executionRates.map((rate) => multiply(rate, stress));
  const base = Array.from({ length: QUANTITY_MAX }, (_, index) => {
    const quantity = index + 1;
    const nominal = simulateQuantity(quantity, marketRates);
    const execution = simulateQuantity(quantity, executionRates);
    const buffered = simulateQuantity(quantity, bufferedRates);
    const steps = marketRates.map((rate, stepIndex) => {
      const executionInput = execution.inputs[stepIndex];
      const executionRate = executionRates[stepIndex];
      const ideal = Number(BigInt(executionInput) * executionRate.numerator) /
        Number(executionRate.denominator);
      const executionOutput = execution.outputs[stepIndex];
      const boundaryHeadroomPercent = executionOutput > 0 && ideal > 0
        ? Math.max(0, (1 - executionOutput / ideal) * 100)
        : 0;
      return {
        nominalInputUnits: nominal.inputs[stepIndex],
        nominalOutputUnits: nominal.outputs[stepIndex],
        nominalExecuted: nominal.executed[stepIndex],
        executionInputUnits: executionInput,
        executionOutputUnits: executionOutput,
        executionExecuted: execution.executed[stepIndex],
        bufferedInputUnits: buffered.inputs[stepIndex],
        bufferedOutputUnits: buffered.outputs[stepIndex],
        bufferedExecuted: buffered.executed[stepIndex],
        boundaryHeadroomPercent,
      };
    });
    const nominalReturnPercent = nominal.complete
      ? (nominal.finalUnits / quantity - 1) * 100
      : null;
    const executionReturnPercent = execution.complete
      ? (execution.finalUnits / quantity - 1) * 100
      : null;
    const bufferedReturnPercent = buffered.complete
      ? (buffered.finalUnits / quantity - 1) * 100
      : null;
    return {
      quantity,
      nominalFinalUnits: nominal.finalUnits,
      executionFinalUnits: execution.finalUnits,
      bufferedFinalUnits: buffered.finalUnits,
      nominalComplete: nominal.complete,
      executionComplete: execution.complete,
      bufferedComplete: buffered.complete,
      ...(nominal.blockedStep !== undefined
        ? { nominalBlockedStep: nominal.blockedStep }
        : {}),
      ...(execution.blockedStep !== undefined
        ? { executionBlockedStep: execution.blockedStep }
        : {}),
      ...(buffered.blockedStep !== undefined
        ? { bufferedBlockedStep: buffered.blockedStep }
        : {}),
      ...(nominal.blockedUnits !== undefined
        ? { nominalBlockedUnits: nominal.blockedUnits }
        : {}),
      ...(execution.blockedUnits !== undefined
        ? { executionBlockedUnits: execution.blockedUnits }
        : {}),
      ...(buffered.blockedUnits !== undefined
        ? { bufferedBlockedUnits: buffered.blockedUnits }
        : {}),
      nominalReturnPercent,
      executionReturnPercent,
      bufferedReturnPercent,
      steps,
      localScore: 0,
      localPeak: false,
      budgetBest: false,
      actionable:
        status === "verified" &&
        !stale &&
        buffered.complete &&
        bufferedReturnPercent !== null &&
        bufferedReturnPercent >= minPercent,
    };
  });

  const radius = Math.max(2, Math.floor(QUANTITY_MAX / 20));
  for (let index = 0; index < base.length; index += 1) {
    const nearby = base.slice(
      Math.max(0, index - radius),
      Math.min(base.length, index + radius + 1),
    ).filter(
      (point) => point.bufferedComplete && point.bufferedReturnPercent !== null,
    ).map((point) => point.bufferedReturnPercent as number);
    const current = base[index];
    if (!current.bufferedComplete || current.bufferedReturnPercent === null || !nearby.length) {
      current.localScore = 0;
      current.localPeak = false;
      continue;
    }
    const low = Math.min(...nearby);
    const high = Math.max(...nearby);
    const value = current.bufferedReturnPercent;
    current.localScore = high - low < 1e-12 ? 0.5 : (value - low) / (high - low);
    current.localPeak =
      value >= high - 1e-12 && nearby.some((other) => value > other + 1e-12);
  }
  for (const [lower, upper] of [[1, 5], [6, 10], [11, 25], [26, 50], [51, 100]]) {
    const candidates = base.slice(lower - 1, upper).filter(
      (point) => point.bufferedComplete && point.bufferedReturnPercent !== null,
    );
    if (!candidates.length) continue;
    const best = candidates.reduce((current, candidate) =>
      (candidate.bufferedReturnPercent as number) >
      (current.bufferedReturnPercent as number)
        ? candidate
        : current,
    );
    best.budgetBest = true;
  }
  return base;
}

function sideItem(side: PairSide | undefined): ExchangeItem | null {
  if (!side?.ApiId) return null;
  const name = side.Text || side.ItemMetadata?.name;
  if (!name) return null;
  const iconUrl = side.IconUrl || side.ItemMetadata?.icon;
  return {
    apiId: side.ApiId,
    name,
    category: side.CategoryApiId ?? "",
    isCurrency: isCurrencyCategory(side.CategoryApiId),
    ...(iconUrl ? { iconUrl } : {}),
  };
}

export function buildRateBook(
  raw: unknown,
  meta: {
    league?: string;
    epoch?: string;
    snapshotId?: number;
    fetchedAt?: number;
    degraded?: boolean;
  } = {},
): RateBook {
  const catalog = new Map<string, ExchangeItem>();
  const rates: RateBook["rates"] = new Map();
  for (const pair of pairsOf(raw)) {
    const one = sideItem(pair.CurrencyOne);
    const two = sideItem(pair.CurrencyTwo);
    if (one) catalog.set(one.apiId, one);
    if (two) catalog.set(two.apiId, two);
    if (!one || !two || !one.isCurrency || !two.isCurrency) {
      continue;
    }
    const oneVolume = Number(pair.CurrencyOneData?.VolumeTraded ?? 0);
    const twoVolume = Number(pair.CurrencyTwoData?.VolumeTraded ?? 0);
    if (
      !Number.isFinite(oneVolume) ||
      !Number.isFinite(twoVolume) ||
      !(oneVolume > 0) ||
      !(twoVolume > 0)
    ) continue;
    const liquidityRaw = Number(pair.Volume ?? 0);
    const liquidity = Number.isFinite(liquidityRaw) && liquidityRaw >= 0
      ? liquidityRaw
      : 0;
    const oneToTwo = twoVolume / oneVolume;
    const current = rates.get(pairKey(one.apiId, two.apiId));
    if (!current || liquidity > current.liquidity) {
      const confidence = scoutConfidence(oneVolume, twoVolume, liquidity);
      rates.set(pairKey(one.apiId, two.apiId), {
        rate: oneToTwo,
        liquidity,
        fromVolume: oneVolume,
        toVolume: twoVolume,
        confidence,
      });
      rates.set(pairKey(two.apiId, one.apiId), {
        rate: 1 / oneToTwo,
        liquidity,
        fromVolume: twoVolume,
        toVolume: oneVolume,
        confidence,
      });
    }
  }
  return {
    league: meta.league ?? "Standard",
    epoch: meta.epoch,
    snapshotId: meta.snapshotId,
    fetchedAt: meta.fetchedAt ?? Date.now(),
    catalog,
    rates,
    degraded: meta.degraded ?? false,
  };
}

function editDistance(a: string, b: string): number {
  const previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diagonal = previous[0];
    previous[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const old = previous[j];
      previous[j] = Math.min(
        previous[j] + 1,
        previous[j - 1] + 1,
        diagonal + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
      diagonal = old;
    }
  }
  return previous[b.length];
}

function nameScore(raw: string, candidate: string): number {
  const a = normName(raw);
  const b = normName(candidate);
  if (!a || !b) return 0;
  if (a === b) return 1;
  return 1 - editDistance(a, b) / Math.max(a.length, b.length);
}

function rankedExchangeItems(
  rawName: string,
  catalog: Map<string, ExchangeItem>,
): { item: ExchangeItem; score: number }[] {
  return [...catalog.values()]
    .map((item) => ({ item, score: nameScore(rawName, item.name) }))
    .sort((a, b) => b.score - a.score || a.item.name.localeCompare(b.item.name));
}

export function resolveExchangeItem(
  rawName: string,
  catalog: Map<string, ExchangeItem>,
): ExchangeItem {
  const ranked = rankedExchangeItems(rawName, catalog);
  const best = ranked[0];
  const second = ranked[1];
  if (!best || best.score < 0.72 || (best.score < 1 && second && best.score - second.score < 0.06)) {
    throw new Error(`ambiguous Currency Exchange item: ${rawName || "unreadable"}`);
  }
  return best.item;
}

function observedExchangeItem(
  rawName: string,
  catalog: Map<string, ExchangeItem>,
): ExchangeItem {
  const ranked = rankedExchangeItems(rawName, catalog);
  // A plausible catalog match that failed only on margin remains ambiguous.
  // Never turn it into a second identity for an existing exchange item.
  if ((ranked[0]?.score ?? 0) >= 0.72) {
    throw new Error(`ambiguous Currency Exchange item: ${rawName || "unreadable"}`);
  }
  const name = rawName
    .normalize("NFKD")
    .replace(/[^\x20-\x7e]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[^a-z0-9]+/i, "")
    .replace(/[^a-z0-9]+$/i, "");
  const normalized = normName(name);
  const words = normalized.split(" ").filter(Boolean);
  if (
    normalized.length < 8 ||
    !words.some((word) => word.length >= 4) ||
    (words.length === 1 && normalized.length < 10)
  ) {
    throw new Error(`ambiguous Currency Exchange item: ${rawName || "unreadable"}`);
  }
  return {
    apiId: `observed:${normalized.replace(/ /g, "-")}`,
    name,
    category: "observed-exchange",
    isCurrency: false,
  };
}

function catalogWithKnownItems(
  book: RateBook,
  knownItems: ExchangeItem[] | undefined,
): RateBook {
  if (!knownItems?.length) return book;
  const catalog = new Map(book.catalog);
  for (const raw of knownItems) {
    const apiId = typeof raw?.apiId === "string" ? raw.apiId.trim() : "";
    const name = typeof raw?.name === "string" ? raw.name.trim() : "";
    if (!apiId || !name) continue;
    catalog.set(apiId, {
      apiId,
      name,
      category: typeof raw.category === "string" ? raw.category : "",
      isCurrency: isCurrencyCategory(raw.category),
      ...(typeof raw.iconUrl === "string" && raw.iconUrl
        ? { iconUrl: raw.iconUrl }
        : {}),
    });
  }
  return { ...book, catalog };
}

export function resolveClosedSetItem(
  rawName: string,
  catalog: Map<string, ExchangeItem>,
  allowedApiIds: string[],
): { item: ExchangeItem; score: number; margin: number } {
  const allowed = new Set(allowedApiIds.filter(Boolean));
  const ranked = [...catalog.values()]
    .filter((item) => allowed.has(item.apiId))
    .map((item) => ({ item, score: nameScore(rawName, item.name) }))
    .sort((a, b) => b.score - a.score || a.item.name.localeCompare(b.item.name));
  const best = ranked[0];
  const secondScore = ranked[1]?.score ?? 0;
  const margin = (best?.score ?? 0) - secondScore;
  if (!best || best.score < 0.82 || (best.score < 1 && margin < 0.10)) {
    throw new Error(`live Currency Exchange item is ambiguous: ${rawName || "unreadable"}`);
  }
  return { item: best.item, score: best.score, margin };
}

export function resolveObservation(
  input: {
    wantText: string;
    haveText: string;
    wantAmount: number;
    haveAmount: number;
    observedAt?: number;
  },
  book: RateBook,
  options: { observeUnknown?: boolean } = {},
): PairObservation {
  const wantAmount = Number(input.wantAmount);
  const haveAmount = Number(input.haveAmount);
  if (
    !Number.isFinite(wantAmount) ||
    !Number.isFinite(haveAmount) ||
    !(wantAmount > 0) ||
    !(haveAmount > 0)
  ) {
    throw new Error("Currency Exchange market ratio is invalid");
  }
  const resolve = options.observeUnknown
    ? (rawName: string) => {
        try {
          return resolveExchangeItem(rawName, book.catalog);
        } catch {
          return observedExchangeItem(rawName, book.catalog);
        }
      }
    : (rawName: string) => resolveExchangeItem(rawName, book.catalog);
  const want = resolve(input.wantText);
  const have = resolve(input.haveText);
  if (want.apiId === have.apiId) throw new Error("Currency Exchange pair has identical items");
  const observedAt = Number(input.observedAt ?? Date.now());
  return {
    id: `${have.apiId}->${want.apiId}`,
    want,
    have,
    wantAmount,
    haveAmount,
    rate: wantAmount / haveAmount,
    observedAt: Number.isFinite(observedAt) ? observedAt : Date.now(),
  };
}

function catalogItem(book: RateBook, apiId: string): ExchangeItem {
  return (
    book.catalog.get(apiId) ?? {
      apiId,
      name: apiId,
      category: "currency",
      isCurrency: true,
    }
  );
}

function snapshotTimeMs(book: RateBook): number {
  if (book.epoch) {
    const numeric = Number(book.epoch);
    if (Number.isFinite(numeric) && numeric > 0) {
      return numeric < 10_000_000_000 ? numeric * 1000 : numeric;
    }
    const parsed = Date.parse(book.epoch);
    if (Number.isFinite(parsed)) return parsed;
  }
  return book.fetchedAt;
}

export function analyzeLoops(
  targetApiId: string,
  observations: PairObservation[],
  book: RateBook,
  options: {
    now?: number;
    minPercent?: number;
    captureMaxAgeMs?: number;
    safetyBufferBps?: number;
    executionConcessionBps?: number;
  } = {},
): ArbAnalysis {
  const now = options.now ?? Date.now();
  const minPercent = options.minPercent ?? DEFAULT_MIN_PERCENT;
  const maxAge = options.captureMaxAgeMs ?? DEFAULT_CAPTURE_MAX_AGE_MS;
  const bufferBps = safetyBufferBps(options.safetyBufferBps);
  const concessionBps = executionConcessionBps(
    options.executionConcessionBps,
  );
  const concession = rational(BigInt(10_000 - concessionBps), 10_000n);
  const ratesAgeMs = Math.max(0, now - snapshotTimeMs(book));
  const ratesStatus: ArbAnalysis["ratesStatus"] = book.degraded
    ? "degraded"
    : ratesAgeMs > SCOUT_MAX_AGE_MS
      ? "stale"
      : "fresh";
  const target = observations
    .flatMap((observation) => [observation.want, observation.have])
    .find((item) => item.apiId === targetApiId) ?? catalogItem(book, targetApiId);
  type GraphEdge = LoopLeg & { observationId?: string };
  const captures: CaptureView[] = [];
  const bridges = new Map<string, BridgeCaptureView>();
  const exactTargetEdges = new Map<string, GraphEdge>();
  const exactBridgeEdges = new Map<string, GraphEdge>();
  const capturedCurrencies = new Map<string, ExchangeItem>();
  const unavailable = new Set<string>();

  const keepNewest = (edges: Map<string, GraphEdge>, edge: GraphEdge): void => {
    const key = pairKey(edge.from.apiId, edge.to.apiId);
    const current = edges.get(key);
    if (!current || Number(edge.observedAt ?? 0) >= Number(current.observedAt ?? 0)) {
      edges.set(key, edge);
    }
  };

  for (const observation of observations) {
    let role: CaptureView["role"] | null = null;
    let quote: ExchangeItem | null = null;
    if (observation.have.apiId === targetApiId) {
      role = "sell";
      quote = observation.want;
    } else if (observation.want.apiId === targetApiId) {
      role = "buy";
      quote = observation.have;
    }
    if (!role || !quote) continue;
    if (!quote.isCurrency) {
      unavailable.add(`${quote.name} is not a supported bridge currency`);
      continue;
    }
    captures.push({
      ...observation,
      role,
      quote,
      stale: now - observation.observedAt > maxAge,
      validUntil: observation.observedAt + maxAge,
    });
    capturedCurrencies.set(quote.apiId, quote);
    keepNewest(exactTargetEdges, {
      from: observation.have,
      to: observation.want,
      rate: observation.rate,
      source: "capture",
      observedAt: observation.observedAt,
      inputAmount: observation.haveAmount,
      outputAmount: observation.wantAmount,
      observationId: observation.id,
    });
  }

  for (const observation of observations) {
    const haveId = observation.have.apiId;
    const wantId = observation.want.apiId;
    if (
      !observation.have.isCurrency ||
      !observation.want.isCurrency ||
      !capturedCurrencies.has(haveId) ||
      !capturedCurrencies.has(wantId) ||
      haveId === wantId
    ) {
      continue;
    }
    const key = pairKey(haveId, wantId);
    const current = bridges.get(key);
    if (!current || observation.observedAt >= current.observedAt) {
      bridges.set(key, {
        ...observation,
        stale: now - observation.observedAt > maxAge,
        validUntil: observation.observedAt + maxAge,
      });
    }
    keepNewest(exactBridgeEdges, {
      from: observation.have,
      to: observation.want,
      rate: observation.rate,
      source: "capture-bridge",
      observedAt: observation.observedAt,
      inputAmount: observation.haveAmount,
      outputAmount: observation.wantAmount,
      observationId: observation.id,
    });
  }

  const currencies = [...capturedCurrencies.values()].sort((a, b) =>
    a.apiId.localeCompare(b.apiId),
  );
  const targetEdge = (from: ExchangeItem, to: ExchangeItem): GraphEdge | undefined => {
    const key = pairKey(from.apiId, to.apiId);
    return exactTargetEdges.get(key);
  };
  const verificationNeeded = new Map<string, VerificationNeed>();
  const noteVerification = (leg: LoopLeg): void => {
    if (leg.source === "poe2scout") {
      verificationNeeded.set(pairKey(leg.from.apiId, leg.to.apiId), {
        from: leg.from,
        to: leg.to,
        hotkey: "Alt+A",
        reason: "poe2scout",
      });
    }
  };
  const loops: ArbLoop[] = [];
  for (const first of currencies) {
    const targetToFirst = targetEdge(target, first);
    if (!targetToFirst) {
      unavailable.add(`${target.name} → ${first.name}`);
      continue;
    }
    for (const second of currencies) {
      if (first.apiId === second.apiId) continue;
      const bridgeKey = pairKey(first.apiId, second.apiId);
      const capturedBridge = exactBridgeEdges.get(bridgeKey);
      const estimatedBridge = ratesStatus === "fresh"
        ? book.rates.get(pairKey(first.apiId, second.apiId))
        : undefined;
      let bridgeLeg: LoopLeg | null = null;
      if (capturedBridge) {
        bridgeLeg = capturedBridge;
      } else if (estimatedBridge) {
        bridgeLeg = {
          from: first,
          to: second,
          rate: estimatedBridge.rate,
          source: "poe2scout",
          scoutEvidence: {
            fromVolume: estimatedBridge.fromVolume,
            toVolume: estimatedBridge.toVolume,
            liquidityExalted: estimatedBridge.liquidity,
            confidence: estimatedBridge.confidence,
          },
        };
      } else {
        unavailable.add(`${first.name} → ${second.name}`);
        continue;
      }
      const secondToTarget = targetEdge(second, target);
      if (!secondToTarget) {
        unavailable.add(`${second.name} → ${target.name}`);
        continue;
      }
      const rawLegs: LoopLeg[] = [
        targetToFirst,
        bridgeLeg,
        secondToTarget,
      ];
      const canonicalRates = rawLegs.map(canonicalLegRate);
      if (canonicalRates.some((rate) => rate === null)) continue;
      const rates = canonicalRates as Rational[];
      const legs = rawLegs.map((leg, index) => ({
        ...leg,
        rate: rationalNumber(rates[index]),
        executionRate: rationalNumber(multiply(rates[index], concession)),
      }));
      let nominalRate = rational(1n, 1n);
      for (const rate of rates) nominalRate = multiply(nominalRate, rate);
      let executionRate = nominalRate;
      for (let index = 0; index < legs.length; index += 1) {
        executionRate = multiply(executionRate, concession);
      }
      const totalStress = rational(BigInt(10_000 - bufferBps), 10_000n);
      const bufferedRate = multiply(executionRate, totalStress);
      const multiplier = rationalNumber(nominalRate);
      const executionMultiplier = rationalNumber(executionRate);
      const bufferedMultiplier = rationalNumber(bufferedRate);
      if (!(multiplier > 0) || !Number.isFinite(multiplier)) continue;
      const liveTimes = legs
        .filter((leg) => leg.source !== "poe2scout")
        .map((leg) => Number(leg.observedAt ?? 0));
      const validUntil = liveTimes.length
        ? Math.min(...liveTimes.map((observedAt) => observedAt + maxAge))
        : undefined;
      const stale = validUntil !== undefined && now > validUntil;
      const status: ArbLoop["status"] = legs.every(
        (leg) => leg.source === "capture" || leg.source === "capture-bridge",
      )
        ? "verified"
        : "estimate";
      const estimateConfidence = status === "estimate"
        ? legs.some(
            (leg) =>
              leg.source === "poe2scout" &&
              leg.scoutEvidence?.confidence === "thin",
          )
          ? "thin"
          : "reliable"
        : undefined;
      const path = [target, first, second, target];
      const percent = (multiplier - 1) * 100;
      const executionPercent = (executionMultiplier - 1) * 100;
      const bufferedPercent = (bufferedMultiplier - 1) * 100;
      const outcomes = quantityOutcomes(
        rates,
        concessionBps,
        bufferBps,
        status,
        stale,
        minPercent,
      );
      for (const leg of legs) noteVerification(leg);
      loops.push({
        id: path.map((item) => item.apiId).join("->"),
        path,
        legs,
        multiplier,
        percent,
        nominalMultiplier: multiplier,
        nominalPercent: percent,
        executionMultiplier,
        executionPercent,
        bufferedMultiplier,
        bufferedPercent,
        status,
        ...(estimateConfidence ? { estimateConfidence } : {}),
        ...(validUntil !== undefined ? { validUntil } : {}),
        stale,
        actionable: outcomes.some((outcome) => outcome.actionable),
        quantityOutcomes: outcomes,
      });
    }
  }

  loops.sort(
    (a, b) =>
      Number(b.status === "verified") - Number(a.status === "verified") ||
      Number(b.estimateConfidence === "reliable") -
        Number(a.estimateConfidence === "reliable") ||
      b.bufferedPercent - a.bufferedPercent ||
      a.id.localeCompare(b.id),
  );
  const bestVerifiedLoop = loops.find(
    (loop) => loop.status === "verified" && !loop.stale,
  );
  const bestCandidateLoop = loops.find(
    (loop) =>
      loop.status === "estimate" &&
      loop.estimateConfidence === "reliable" &&
      !loop.stale,
  );
  return {
    target,
    captures: captures.sort((a, b) => b.observedAt - a.observedAt),
    bridges: [...bridges.values()].sort((a, b) => b.observedAt - a.observedAt),
    loops,
    ...(bestVerifiedLoop ? { bestVerifiedLoop } : {}),
    ...(bestCandidateLoop ? { bestCandidateLoop } : {}),
    loopsEvaluated: loops.length,
    capturedCurrencyCount: currencies.length,
    unavailable: [...unavailable].sort(),
    verificationNeeded: [...verificationNeeded.values()].sort((a, b) =>
      pairKey(a.from.apiId, a.to.apiId).localeCompare(pairKey(b.from.apiId, b.to.apiId)),
    ),
    ratesEpoch: book.epoch,
    ...(book.snapshotId ? { ratesSnapshotId: book.snapshotId } : {}),
    ratesFetchedAt: book.fetchedAt,
    ratesAgeMs,
    ratesStatus,
    safetyBufferBps: bufferBps,
    perLegSafetyBufferBps:
      (1 - rationalNumber(perLegStress(bufferBps, 3))) * 10_000,
    executionConcessionBps: concessionBps,
    executionConcessionLoopPercent:
      (1 - rationalNumber(multiply(multiply(concession, concession), concession))) * 100,
    analyzedAt: now,
  };
}

async function rateBook(
  league: string,
  options: { force?: boolean; reuse?: boolean } = {},
): Promise<RateBook> {
  const cached = cachedBooks.get(league);
  if (options.reuse && cached) return cached;
  if (!options.force && cached) {
    const epoch = await exchangeSnapshotEpoch(league).catch(() => null);
    if (epoch && cached.epoch === epoch) return cached;
    if (!epoch) return { ...cached, degraded: true };
  }
  try {
    const snapshot = await exchangePairSnapshot(league, { force: true });
    const book = buildRateBook(snapshot.pairs, {
      league,
      epoch: snapshot.epoch,
      snapshotId: snapshot.snapshotId,
      fetchedAt: snapshot.fetchedAt,
      degraded: false,
    });
    cachedBooks.set(league, book);
    return book;
  } catch (error) {
    if (cached) return { ...cached, degraded: true };
    throw error;
  }
}

export async function arbPair(input: {
  league: string;
  wantText: string;
  haveText: string;
  wantAmount: number;
  haveAmount: number;
  observedAt?: number;
  forceRates?: boolean;
  knownItems?: ExchangeItem[];
}): Promise<{ observation: PairObservation; ratesEpoch?: string; ratesFetchedAt: number }> {
  const baseBook = await rateBook(input.league, { force: input.forceRates });
  const book = catalogWithKnownItems(baseBook, input.knownItems);
  return {
    observation: resolveObservation(input, book, { observeUnknown: true }),
    ratesEpoch: book.epoch,
    ratesFetchedAt: book.fetchedAt,
  };
}

export function arbResolveLive(input: {
  league: string;
  allowedApiIds: string[];
  wantText: string;
  haveText: string;
  wantAmount: number;
  haveAmount: number;
  observedAt?: number;
  knownItems?: ExchangeItem[];
}): {
  observation: PairObservation;
  wantScore: number;
  haveScore: number;
  wantMargin: number;
  haveMargin: number;
} {
  const baseBook = cachedBooks.get(input.league);
  if (!baseBook) {
    throw new Error("live arbitrage catalog is unavailable; recalculate first");
  }
  const book = catalogWithKnownItems(baseBook, input.knownItems);
  const want = resolveClosedSetItem(input.wantText, book.catalog, input.allowedApiIds);
  const have = resolveClosedSetItem(input.haveText, book.catalog, input.allowedApiIds);
  const wantAmount = Number(input.wantAmount);
  const haveAmount = Number(input.haveAmount);
  if (
    !Number.isFinite(wantAmount) ||
    !Number.isFinite(haveAmount) ||
    !(wantAmount > 0) ||
    !(haveAmount > 0)
  ) {
    throw new Error("Currency Exchange market ratio is invalid");
  }
  if (want.item.apiId === have.item.apiId) {
    throw new Error("Currency Exchange pair has identical items");
  }
  const observedAt = Number(input.observedAt ?? Date.now());
  return {
    observation: {
      id: `${have.item.apiId}->${want.item.apiId}`,
      want: want.item,
      have: have.item,
      wantAmount,
      haveAmount,
      rate: wantAmount / haveAmount,
      observedAt: Number.isFinite(observedAt) ? observedAt : Date.now(),
    },
    wantScore: want.score,
    haveScore: have.score,
    wantMargin: want.margin,
    haveMargin: have.margin,
  };
}

export async function arbAnalyze(input: {
  league: string;
  targetApiId: string;
  observations: PairObservation[];
  minPercent?: number;
  captureMaxAgeMs?: number;
  safetyBufferBps?: number;
  executionConcessionBps?: number;
  forceRates?: boolean;
  reuseRates?: boolean;
}): Promise<ArbAnalysis> {
  const book = await rateBook(input.league, {
    force: input.forceRates,
    reuse: input.reuseRates,
  });
  return analyzeLoops(input.targetApiId, input.observations, book, {
    minPercent: input.minPercent,
    captureMaxAgeMs: input.captureMaxAgeMs,
    safetyBufferBps: input.safetyBufferBps,
    executionConcessionBps: input.executionConcessionBps,
  });
}

export function _clearArbCache(): void {
  cachedBooks.clear();
}
