import { describe, expect, it } from "vitest";
import {
  analyzeLoops,
  buildRateBook,
  isCurrencyCategory,
  resolveExchangeItem,
  resolveClosedSetItem,
  resolveObservation,
} from "../src/arbitrage";

type Side = { ApiId: string; Text: string; CategoryApiId: string };

function pair(
  one: Side,
  two: Side,
  oneVolume: number,
  twoVolume: number,
  volume = 100,
) {
  return {
    CurrencyOne: one,
    CurrencyTwo: two,
    CurrencyOneData: { VolumeTraded: oneVolume },
    CurrencyTwoData: { VolumeTraded: twoVolume },
    Volume: volume,
  };
}

const chaos = { ApiId: "chaos", Text: "Chaos Orb", CategoryApiId: "currency" };
const exalted = { ApiId: "exalted", Text: "Exalted Orb", CategoryApiId: "currency" };
const divine = { ApiId: "divine", Text: "Divine Orb", CategoryApiId: "currency" };
const annul = { ApiId: "annul", Text: "Orb of Annulment", CategoryApiId: "currency" };
const greaterChaos = {
  ApiId: "greater-chaos-orb",
  Text: "Greater Chaos Orb",
  CategoryApiId: "currency",
};
const greaterExalted = {
  ApiId: "greater-exalted-orb",
  Text: "Greater Exalted Orb",
  CategoryApiId: "currency",
};
const perfectExalted = {
  ApiId: "perfect-exalted-orb",
  Text: "Perfect Exalted Orb",
  CategoryApiId: "currency",
};
const fracturing = {
  ApiId: "fracturing-orb",
  Text: "Fracturing Orb",
  CategoryApiId: "currency",
};
const alchemy = {
  ApiId: "alch",
  Text: "Orb of Alchemy",
  CategoryApiId: "currency",
};
const omen = { ApiId: "omen-whittling", Text: "Omen of Whittling", CategoryApiId: "omens" };
const kulemak = {
  ApiId: "kulemaks-invitation",
  Text: "Kulemak's Invitation",
  CategoryApiId: "fragments",
};

function book() {
  return buildRateBook(
    [
      pair(chaos, exalted, 1500, 100, 10_000),
      pair(omen, chaos, 1, 81, 80),
      pair(omen, exalted, 1, 5, 70),
    ],
    { league: "Test", epoch: "2099-07-21T20:00:00Z", fetchedAt: 1_000 },
  );
}

function observations(now = 10_000) {
  const rates = book();
  return [
    resolveObservation(
      {
        wantText: "Chaos Orb",
        haveText: "Omen of Whittling",
        wantAmount: 81,
        haveAmount: 1,
        observedAt: now,
      },
      rates,
    ),
    resolveObservation(
      {
        wantText: "Omen of Whittling",
        haveText: "Exalted Orb",
        wantAmount: 1,
        haveAmount: 5,
        observedAt: now,
      },
      rates,
    ),
  ];
}

describe("Currency Exchange pair resolution", () => {
  it("preserves I HAVE to I WANT direction", () => {
    const result = resolveObservation(
      {
        wantText: "Chaos Orb",
        haveText: "Omen of Whittling",
        wantAmount: 81,
        haveAmount: 1,
        observedAt: 123,
      },
      book(),
    );
    expect(result.id).toBe("omen-whittling->chaos");
    expect(result.rate).toBe(81);
    expect(result.observedAt).toBe(123);
  });

  it("recovers a small OCR error but rejects ambiguous text", () => {
    expect(resolveExchangeItem("Omen of WhittIing", book().catalog).apiId).toBe(
      "omen-whittling",
    );
    expect(resolveExchangeItem("Omen of hittling", book().catalog).apiId).toBe(
      "omen-whittling",
    );
    expect(() => resolveExchangeItem("Orb", book().catalog)).toThrow(/ambiguous/);
  });

  it("rejects invalid ratios and self-pairs", () => {
    expect(() =>
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Exalted Orb", wantAmount: 0, haveAmount: 1 },
        book(),
      ),
    ).toThrow(/ratio/);
    expect(() =>
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 1 },
        book(),
      ),
    ).toThrow(/identical/);
    expect(() =>
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Exalted Orb", wantAmount: Infinity, haveAmount: 1 },
        book(),
      ),
    ).toThrow(/ratio/);
  });

  it("resolves live OCR only inside the selected loop", () => {
    const rates = book();
    expect(
      resolveClosedSetItem(
        "Omen of WhittIing",
        rates.catalog,
        ["omen-whittling", "chaos", "exalted"],
      ).item.apiId,
    ).toBe("omen-whittling");
    expect(() =>
      resolveClosedSetItem("Divine Orb", rates.catalog, ["chaos", "exalted"]),
    ).toThrow(/ambiguous/);
  });
});

describe("currency bridge extraction", () => {
  it("tracks every catalog currency pair rather than a fixed allowlist", () => {
    const sides = [
      chaos,
      exalted,
      divine,
      annul,
      greaterChaos,
      greaterExalted,
      perfectExalted,
      fracturing,
      alchemy,
    ];
    const pairs = sides.flatMap((one, index) =>
      sides.slice(index + 1).map((two, offset) =>
        pair(one, two, 10 + index, 20 + offset, 100 + offset),
      ),
    );
    const rates = buildRateBook(pairs).rates;
    expect(pairs).toHaveLength(36);
    expect(rates.size).toBe(72);
    for (const side of sides) expect(rates.has(`${side.ApiId}->${side.ApiId}`)).toBe(false);
  });

  it("accepts only the currency category and excludes other exchange commodities", () => {
    const essence = {
      ApiId: "essence-of-opulence",
      Text: "Essence of Opulence",
      CategoryApiId: "essences",
    };
    const lineage = {
      ApiId: "lineage-support",
      Text: "Lineage Support",
      CategoryApiId: "lineage-support-gems",
    };
    const rates = buildRateBook([
      pair(fracturing, perfectExalted, 10, 2, 50_000),
      pair(essence, fracturing, 10, 2, 50_000),
      pair(lineage, fracturing, 10, 2, 50_000),
    ]);

    expect(isCurrencyCategory("currency")).toBe(true);
    expect(isCurrencyCategory(" Currency ")).toBe(true);
    expect(isCurrencyCategory("essences")).toBe(false);
    expect(rates.rates.has("fracturing-orb->perfect-exalted-orb")).toBe(true);
    expect(rates.rates.has("essence-of-opulence->fracturing-orb")).toBe(false);
    expect(rates.rates.has("lineage-support->fracturing-orb")).toBe(false);
    expect(rates.catalog.get("essence-of-opulence")?.isCurrency).toBe(false);
    expect(rates.catalog.get("lineage-support")?.isCurrency).toBe(false);
  });

  it("stores one direct pair as two reciprocal directions and no self market", () => {
    const rates = book().rates;
    expect(rates.get("chaos->exalted")?.rate).toBeCloseTo(1 / 15);
    expect(rates.get("exalted->chaos")?.rate).toBeCloseTo(15);
    expect(rates.has("exalted->exalted")).toBe(false);
  });

  it("drops non-finite pair volumes before they can poison loop math", () => {
    const rates = buildRateBook([
      pair(chaos, exalted, Infinity, 1, 10_000),
      pair(divine, exalted, 100, Number.NaN, 10_000),
    ]).rates;
    expect(rates.size).toBe(0);
  });

  it("keeps the most-liquid duplicate direct pair", () => {
    const rates = buildRateBook([
      pair(chaos, exalted, 10, 1, 5),
      pair(chaos, exalted, 20, 1, 50),
    ]).rates;
    expect(rates.get("chaos->exalted")?.rate).toBeCloseTo(0.05);
  });
});

describe("loop analysis", () => {
  it("ranks a Poe2Scout bridge as an unverified candidate", () => {
    const result = analyzeLoops("omen-whittling", observations(), book(), {
      now: 10_500,
      minPercent: 5,
    });
    expect(result.bestCandidateLoop?.path.map((item) => item.name)).toEqual([
      "Omen of Whittling",
      "Chaos Orb",
      "Exalted Orb",
      "Omen of Whittling",
    ]);
    expect(result.bestCandidateLoop?.multiplier).toBeCloseTo(1.08);
    expect(result.bestCandidateLoop?.percent).toBeCloseTo(8);
    expect(result.bestCandidateLoop?.status).toBe("estimate");
    expect(result.bestCandidateLoop?.actionable).toBe(false);
    expect(result.bestCandidateLoop?.legs.map((leg) => leg.source)).toEqual([
      "capture",
      "poe2scout",
      "capture",
    ]);
  });

  it("keeps thin Poe2Scout bridges visible but never selects them as best", () => {
    const rates = buildRateBook(
      [
        pair(chaos, exalted, 1500, 100, 9_999),
        pair(omen, chaos, 1, 81),
        pair(omen, exalted, 1, 5),
      ],
      { epoch: "2099-01-01T00:00:00Z" },
    );
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Omen of Whittling", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 81 },
        rates,
      ),
      resolveObservation(
        { wantText: "Omen of Whittling", haveText: "Exalted Orb", wantAmount: 1, haveAmount: 5 },
        rates,
      ),
    ];

    const result = analyzeLoops("omen-whittling", seen, rates);
    expect(result.loops).toHaveLength(2);
    expect(result.loops.every((loop) => loop.estimateConfidence === "thin")).toBe(true);
    expect(result.bestCandidateLoop).toBeUndefined();
    expect(result.verificationNeeded.some((need) => need.reason === "poe2scout")).toBe(true);
  });

  it("disables stale Poe2Scout bridges instead of extrapolating them", () => {
    const rates = buildRateBook(
      [
        pair(chaos, exalted, 1500, 100, 10_000),
        pair(omen, chaos, 1, 81),
        pair(omen, exalted, 1, 5),
      ],
      { epoch: "100" },
    );
    const now = 100_000 + 4 * 60 * 60 * 1000 + 1;
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1, observedAt: now },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1, observedAt: now },
        rates,
      ),
    ];

    const result = analyzeLoops("omen-whittling", seen, rates, { now });
    expect(result.ratesStatus).toBe("stale");
    expect(result.loops).toHaveLength(0);
    expect(result.bestCandidateLoop).toBeUndefined();
  });

  it("makes a loop verified only when every executable direction was captured", () => {
    const rates = book();
    const forward = resolveObservation(
      {
        wantText: "Exalted Orb",
        haveText: "Chaos Orb",
        wantAmount: 1,
        haveAmount: 10,
        observedAt: 10_400,
      },
      rates,
    );
    const reverse = resolveObservation(
      {
        wantText: "Chaos Orb",
        haveText: "Exalted Orb",
        wantAmount: 10,
        haveAmount: 1,
        observedAt: 10_400,
      },
      rates,
    );
    const fromForward = analyzeLoops("omen-whittling", [...observations(), forward], rates, {
      now: 10_500,
    });
    const fromReverse = analyzeLoops("omen-whittling", [...observations(), reverse], rates, {
      now: 10_500,
    });

    expect(fromForward.bridges).toHaveLength(1);
    expect(fromForward.bestVerifiedLoop?.percent).toBeCloseTo(62);
    expect(fromForward.bestVerifiedLoop?.actionable).toBe(true);
    expect(fromForward.bestCandidateLoop).toBeUndefined();
    expect(fromReverse.bestVerifiedLoop).toBeUndefined();
    expect(fromReverse.loops).toHaveLength(1);
    expect(fromReverse.bestCandidateLoop?.legs[1].source).toBe("poe2scout");
  });

  it("keeps both live bridge directions independently", () => {
    const rates = book();
    const oldBridge = resolveObservation(
      {
        wantText: "Exalted Orb",
        haveText: "Chaos Orb",
        wantAmount: 1,
        haveAmount: 10,
        observedAt: 10_100,
      },
      rates,
    );
    const newBridge = resolveObservation(
      {
        wantText: "Chaos Orb",
        haveText: "Exalted Orb",
        wantAmount: 20,
        haveAmount: 1,
        observedAt: 10_400,
      },
      rates,
    );
    const result = analyzeLoops(
      "omen-whittling",
      [...observations(), oldBridge, newBridge],
      rates,
      { now: 10_500 },
    );

    expect(result.bridges.map((bridge) => bridge.id).sort()).toEqual([
      "chaos->exalted",
      "exalted->chaos",
    ]);
    expect(result.bestVerifiedLoop?.legs[1].rate).toBeCloseTo(0.1);
  });

  it("uses only exact target directions", () => {
    const rates = book();
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 100, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Omen of Whittling", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 50, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
    ];
    const loop = analyzeLoops("omen-whittling", seen, rates, { now: 1_100 }).loops[0];
    expect(loop.path.map((item) => item.apiId)).toEqual([
      "omen-whittling",
      "exalted",
      "chaos",
      "omen-whittling",
    ]);
    expect(loop.legs.map((leg) => leg.source)).toEqual([
      "capture",
      "poe2scout",
      "capture",
    ]);
    expect(loop.percent).toBeCloseTo(50);
    expect(loop.status).toBe("estimate");
  });

  it("does not infer reverse markets from one-sided Alt+A captures", () => {
    const rates = book();
    const seen = [
      resolveObservation(
        {
          wantText: "Chaos Orb",
          haveText: "Omen of Whittling",
          wantAmount: 100,
          haveAmount: 1,
          observedAt: 1_000,
        },
        rates,
      ),
      resolveObservation(
        {
          wantText: "Exalted Orb",
          haveText: "Omen of Whittling",
          wantAmount: 5,
          haveAmount: 1,
          observedAt: 1_000,
        },
        rates,
      ),
    ];
    const result = analyzeLoops("omen-whittling", seen, rates, { now: 1_100 });
    expect(result.loops).toHaveLength(0);
    expect(result.bestVerifiedLoop).toBeUndefined();
    expect(result.bestCandidateLoop).toBeUndefined();
    expect(result.unavailable).toEqual([
      "Chaos Orb → Omen of Whittling",
      "Exalted Orb → Omen of Whittling",
    ]);
  });

  it("keeps a 1:20 Raven market distinct from its independently captured 1:3 reverse", () => {
    const raven = {
      ApiId: "ravens-reflection",
      Text: "Raven's Reflection",
      CategoryApiId: "omens",
    };
    const rates = buildRateBook([
      pair(raven, greaterChaos, 20, 1, 10_000),
      pair(raven, chaos, 1, 10, 10_000),
      pair(greaterChaos, chaos, 1, 10, 10_000),
    ]);
    const oneSided = [
      resolveObservation(
        { wantText: "Greater Chaos Orb", haveText: "Raven's Reflection", wantAmount: 1, haveAmount: 20 },
        rates,
      ),
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Raven's Reflection", wantAmount: 10, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Raven's Reflection", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 10 },
        rates,
      ),
    ];
    const withoutReverse = analyzeLoops("ravens-reflection", oneSided, rates);

    expect(
      withoutReverse.loops.find(
        (loop) => loop.path[1].apiId === "chaos" && loop.path[2].apiId === "greater-chaos-orb",
      ),
    ).toBeUndefined();
    expect(withoutReverse.unavailable).toContain(
      "Greater Chaos Orb → Raven's Reflection",
    );

    const reverse = resolveObservation(
      { wantText: "Raven's Reflection", haveText: "Greater Chaos Orb", wantAmount: 3, haveAmount: 1 },
      rates,
    );
    const withReverse = analyzeLoops("ravens-reflection", [...oneSided, reverse], rates);
    const loop = withReverse.loops.find(
      (candidate) =>
        candidate.path[1].apiId === "chaos" &&
        candidate.path[2].apiId === "greater-chaos-orb",
    );

    expect(loop?.legs[2].source).toBe("capture");
    expect(loop?.legs[2].rate).toBe(3);
    expect(loop?.legs[2].rate).not.toBe(20);
  });

  it("waits for a second captured currency before evaluating item arbitrage", () => {
    const result = analyzeLoops("omen-whittling", observations().slice(0, 1), book());
    expect(result.capturedCurrencyCount).toBe(1);
    expect(result.loopsEvaluated).toBe(0);
    expect(result.bestVerifiedLoop).toBeUndefined();
    expect(result.bestCandidateLoop).toBeUndefined();
  });

  it("marks old observations stale and non-actionable", () => {
    const result = analyzeLoops("omen-whittling", observations(1_000), book(), {
      now: 200_000,
      minPercent: 5,
      captureMaxAgeMs: 120_000,
    });
    expect(result.loops[0].stale).toBe(true);
    expect(result.loops[0].actionable).toBe(false);
    expect(result.bestVerifiedLoop).toBeUndefined();
    expect(result.bestCandidateLoop).toBeUndefined();
  });

  it("reports snapshot age from the exchange epoch rather than HTTP fetch time", () => {
    const rates = buildRateBook(
      [pair(chaos, exalted, 15, 1), pair(omen, chaos, 1, 81), pair(omen, exalted, 1, 5)],
      { epoch: "100", fetchedAt: 199_000 },
    );
    const result = analyzeLoops("omen-whittling", observations(199_000), rates, {
      now: 200_000,
    });
    expect(result.ratesAgeMs).toBe(100_000);
    expect(result.analyzedAt).toBe(200_000);
  });

  it("does not synthesize a missing bridge through another currency", () => {
    const rates = buildRateBook([
      pair(chaos, divine, 1000, 1),
      pair(divine, exalted, 1, 200),
      pair(omen, chaos, 1, 81),
      pair(omen, exalted, 1, 5),
    ]);
    const result = analyzeLoops("omen-whittling", observations(), rates, { now: 10_500 });
    expect(result.loops).toHaveLength(0);
    expect(result.unavailable).toEqual([
      "Chaos Orb → Exalted Orb",
      "Omen of Whittling → Exalted Orb",
    ]);
  });

  it("rejects the reported Kulemak 300 percent loop without its return market", () => {
    const rates = buildRateBook([
      pair(kulemak, divine, 1, 3.43),
      pair(kulemak, annul, 1, 1.5),
      pair(divine, annul, 4, 7),
    ]);
    const seen = [
      resolveObservation(
        {
          wantText: "Divine Orb",
          haveText: "Kulemak's Invitation",
          wantAmount: 3.43,
          haveAmount: 1,
          observedAt: 10_000,
        },
        rates,
      ),
      resolveObservation(
        {
          wantText: "Orb of Annulment",
          haveText: "Kulemak's Invitation",
          wantAmount: 1.5,
          haveAmount: 1,
          observedAt: 10_000,
        },
        rates,
      ),
      resolveObservation(
        {
          wantText: "Orb of Annulment",
          haveText: "Divine Orb",
          wantAmount: 1.75,
          haveAmount: 1,
          observedAt: 10_000,
        },
        rates,
      ),
    ];
    const result = analyzeLoops("kulemaks-invitation", seen, rates, { now: 10_100 });
    const suspicious = result.loops.find(
      (loop) => loop.path[1].apiId === "divine" && loop.path[2].apiId === "annul",
    );
    expect(suspicious).toBeUndefined();
    expect(result.loops).toHaveLength(0);
    expect(result.bestVerifiedLoop).toBeUndefined();
    expect(result.unavailable).toEqual(expect.arrayContaining([
      "Divine Orb → Kulemak's Invitation",
      "Orb of Annulment → Kulemak's Invitation",
    ]));
  });

  it("computes the Kulemak loop from a separately captured return market", () => {
    const rates = buildRateBook([
      pair(kulemak, divine, 1, 3.43),
      pair(kulemak, annul, 10, 59),
      pair(divine, annul, 4, 7),
    ]);
    const seen = [
      resolveObservation(
        { wantText: "Divine Orb", haveText: "Kulemak's Invitation", wantAmount: 3.43, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Orb of Annulment", haveText: "Divine Orb", wantAmount: 1.75, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: "Orb of Annulment", wantAmount: 10, haveAmount: 59 },
        rates,
      ),
    ];
    const result = analyzeLoops("kulemaks-invitation", seen, rates);
    expect(result.bestVerifiedLoop?.multiplier).toBeCloseTo(3.43 * 1.75 * (10 / 59));
    expect(result.bestVerifiedLoop?.percent).toBeLessThan(2);
    expect(result.bestVerifiedLoop?.percent).toBeGreaterThan(1);
    expect(result.bestVerifiedLoop?.actionable).toBe(false);
  });

  it("recomputes the complete finite search as captured currencies are added", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 10),
      pair(kulemak, exalted, 1, 20),
      pair(kulemak, divine, 1, 30),
      pair(chaos, exalted, 10, 20),
      pair(chaos, divine, 10, 30),
      pair(exalted, divine, 20, 30),
    ]);
    const byCurrency = [chaos, exalted, divine].flatMap((currency, index) => [
      resolveObservation(
        { wantText: currency.Text, haveText: "Kulemak's Invitation", wantAmount: (index + 1) * 10, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: currency.Text, wantAmount: 1, haveAmount: (index + 1) * 10 },
        rates,
      ),
    ]);
    const seen = byCurrency;
    expect(analyzeLoops("kulemaks-invitation", byCurrency.slice(0, 2), rates).loopsEvaluated).toBe(0);
    expect(analyzeLoops("kulemaks-invitation", byCurrency.slice(0, 4), rates).loopsEvaluated).toBe(2);
    expect(analyzeLoops("kulemaks-invitation", seen, rates).loopsEvaluated).toBe(6);
  });

  it("never routes through a currency that was not captured", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 10),
      pair(kulemak, exalted, 1, 20),
      pair(chaos, exalted, 10, 20),
      pair(chaos, annul, 10, 2),
      pair(annul, exalted, 2, 20),
    ]);
    const seen = [chaos, exalted].flatMap((currency, index) => [
      resolveObservation(
        { wantText: currency.Text, haveText: "Kulemak's Invitation", wantAmount: (index + 1) * 10, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: currency.Text, wantAmount: 1, haveAmount: (index + 1) * 10 },
        rates,
      ),
    ]);
    const result = analyzeLoops("kulemaks-invitation", seen, rates);
    expect(result.loopsEvaluated).toBe(2);
    expect(result.loops.every((loop) => loop.path.every((item) => item.apiId !== "annul"))).toBe(true);
  });

  it("enumerates exactly twenty directed pairwise loops for five captured currencies", () => {
    const majors = [chaos, exalted, divine, annul, greaterChaos];
    const raw = [
      ...majors.map((currency, index) => pair(kulemak, currency, 1, index + 2)),
      ...majors.flatMap((one, index) =>
        majors.slice(index + 1).map((two, offset) =>
          pair(one, two, index + 2, index + offset + 3),
        ),
      ),
    ];
    const rates = buildRateBook(raw);
    const seen = majors.flatMap((currency, index) => [
      resolveObservation(
        { wantText: currency.Text, haveText: "Kulemak's Invitation", wantAmount: index + 2, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: currency.Text, wantAmount: 1, haveAmount: index + 2 },
        rates,
      ),
    ]);
    const result = analyzeLoops("kulemaks-invitation", seen, rates);
    expect(result.capturedCurrencyCount).toBe(5);
    expect(result.loopsEvaluated).toBe(20);
    expect(result.loops).toHaveLength(20);
    expect(result.loops.every((loop) => loop.path.length === 4)).toBe(true);
    expect(JSON.stringify(result).length).toBeLessThan(4 * 1024 * 1024);
  });

  it("requires exact return markets after two one-sided Alt+A captures", () => {
    const rates = book();
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
    ];
    const result = analyzeLoops("omen-whittling", seen, rates, { now: 10_100 });

    expect(result.loops).toHaveLength(0);
    expect(result.bestCandidateLoop).toBeUndefined();
    expect(result.unavailable).toEqual([
      "Chaos Orb → Omen of Whittling",
      "Exalted Orb → Omen of Whittling",
    ]);
  });

  it("does not certify a loop when Alt+A refined only its currency bridge", () => {
    const rates = book();
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 15, observedAt: 10_050 },
        rates,
      ),
    ];
    const result = analyzeLoops("omen-whittling", seen, rates, { now: 10_100 });
    const loop = result.loops.find((candidate) => candidate.path[1].apiId === "chaos");

    expect(loop).toBeUndefined();
    expect(result.loops).toHaveLength(0);
    expect(result.bestVerifiedLoop).toBeUndefined();
  });

  it("completes a loop only after an exact reverse target capture", () => {
    const rates = book();
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1, observedAt: 10_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Omen of Whittling", haveText: "Exalted Orb", wantAmount: 10, haveAmount: 59, observedAt: 10_050 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 15, observedAt: 10_050 },
        rates,
      ),
    ];
    const result = analyzeLoops("omen-whittling", seen, rates, { now: 10_100 });
    const loop = result.loops.find((candidate) => candidate.path[1].apiId === "chaos");

    expect(loop?.legs[2].source).toBe("capture");
    expect(loop?.legs[2].rate).toBeCloseTo(10 / 59);
    expect(loop?.status).toBe("verified");
  });

  it("keeps every enumerated loop connected and its percentage equal to the leg product", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 17),
      pair(kulemak, exalted, 1, 31),
      pair(kulemak, divine, 1, 2.7),
      pair(chaos, exalted, 170, 31),
      pair(chaos, divine, 170, 2.7),
      pair(exalted, divine, 31, 2.7),
    ]);
    const seen = [chaos, exalted, divine].flatMap((currency, index) => [
      resolveObservation(
        { wantText: currency.Text, haveText: "Kulemak's Invitation", wantAmount: [17, 31, 2.7][index], haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: currency.Text, wantAmount: 1, haveAmount: [17, 31, 2.7][index] },
        rates,
      ),
    ]);
    const result = analyzeLoops("kulemaks-invitation", seen, rates);

    expect(result.loops).toHaveLength(6);
    for (const loop of result.loops) {
      expect(loop.legs).toHaveLength(3);
      for (let index = 0; index < loop.legs.length; index += 1) {
        expect(loop.legs[index].from.apiId).toBe(loop.path[index].apiId);
        expect(loop.legs[index].to.apiId).toBe(loop.path[index + 1].apiId);
      }
      const product = loop.legs.reduce((value, leg) => value * leg.rate, 1);
      expect(loop.multiplier).toBeCloseTo(product, 12);
      expect(loop.percent).toBeCloseTo((product - 1) * 100, 10);
      expect(loop.actionable).toBe(false);
    }
  });

  it("deduplicates verification work across all exploratory loops", () => {
    const rates = book();
    const result = analyzeLoops(
      "omen-whittling",
      [
        resolveObservation(
          { wantText: "Chaos Orb", haveText: "Omen of Whittling", wantAmount: 81, haveAmount: 1 },
          rates,
        ),
        resolveObservation(
          { wantText: "Exalted Orb", haveText: "Omen of Whittling", wantAmount: 5, haveAmount: 1 },
          rates,
        ),
        resolveObservation(
          { wantText: "Omen of Whittling", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 81 },
          rates,
        ),
        resolveObservation(
          { wantText: "Omen of Whittling", haveText: "Exalted Orb", wantAmount: 1, haveAmount: 5 },
          rates,
        ),
      ],
      rates,
    );
    const keys = result.verificationNeeded.map(
      (need) => `${need.from.apiId}->${need.to.apiId}`,
    );

    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toEqual([
      "chaos->exalted",
      "exalted->chaos",
    ]);
    expect(result.verificationNeeded.every((need) => need.hotkey === "Alt+A")).toBe(true);
  });

  it("returns buffered whole-unit outcomes for every supported slider quantity", () => {
    const result = analyzeLoops("omen-whittling", observations(), book(), {
      now: 10_500,
      executionConcessionBps: 0,
    });
    expect(result.loops[0].quantityOutcomes).toHaveLength(100);
    expect(result.loops[0].quantityOutcomes[0].quantity).toBe(1);
    expect(result.loops[0].quantityOutcomes[99].quantity).toBe(100);
    expect(result.loops[0].bufferedMultiplier).toBeCloseTo(
      result.loops[0].nominalMultiplier * 0.95,
    );
    expect(result.perLegSafetyBufferBps).toBeCloseTo(169.5, 1);
  });

  it("distributes one total loop buffer across legs before whole-unit flooring", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 1.01),
      pair(chaos, exalted, 1, 1),
    ]);
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Kulemak's Invitation", wantAmount: 1.01, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: "Exalted Orb", wantAmount: 1, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
    ];
    const loop = analyzeLoops("kulemaks-invitation", seen, rates, {
      now: 1_100,
      safetyBufferBps: 500,
      executionConcessionBps: 0,
    }).bestVerifiedLoop;

    expect(loop?.nominalPercent).toBeCloseTo(1);
    expect(loop?.bufferedPercent).toBeCloseTo((1.01 * 0.95 - 1) * 100);
    expect(loop?.quantityOutcomes[0].nominalFinalUnits).toBe(1);
    expect(loop?.quantityOutcomes[0].bufferedFinalUnits).toBe(0);
    expect(loop?.quantityOutcomes[0].nominalComplete).toBe(true);
    expect(loop?.quantityOutcomes[0].bufferedComplete).toBe(false);
    expect(loop?.quantityOutcomes[0].bufferedReturnPercent).toBeNull();
    expect(loop?.quantityOutcomes[0].bufferedBlockedStep).toBe(0);
    expect(loop?.quantityOutcomes[0].bufferedBlockedUnits).toBe(1);
    expect(loop?.quantityOutcomes[0].steps[0].boundaryHeadroomPercent).toBeCloseTo(
      100 * (1 - 1 / 1.01),
    );
    expect(loop?.quantityOutcomes[0].actionable).toBe(false);
  });

  it("makes actionability quantity-specific and preserves zero-buffer behavior", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 10, 1),
      pair(chaos, exalted, 1, 20),
    ]);
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Kulemak's Invitation", wantAmount: 0.1, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 20, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: "Exalted Orb", wantAmount: 0.6, haveAmount: 1, observedAt: 1_000 },
        rates,
      ),
    ];
    const unbuffered = analyzeLoops("kulemaks-invitation", seen, rates, {
      now: 1_100,
      safetyBufferBps: 0,
      executionConcessionBps: 0,
    }).bestVerifiedLoop;
    const buffered = analyzeLoops("kulemaks-invitation", seen, rates, {
      now: 1_100,
      safetyBufferBps: 500,
      executionConcessionBps: 0,
    }).bestVerifiedLoop;

    expect(unbuffered?.nominalPercent).toBeCloseTo(20);
    expect(unbuffered?.quantityOutcomes.slice(0, 9).every((point) => !point.actionable)).toBe(true);
    expect(unbuffered?.quantityOutcomes[9].nominalFinalUnits).toBe(12);
    expect(unbuffered?.quantityOutcomes[9].bufferedFinalUnits).toBe(12);
    expect(unbuffered?.quantityOutcomes[9].actionable).toBe(true);
    expect(buffered?.quantityOutcomes[9].bufferedFinalUnits).toBe(0);
    expect(buffered?.quantityOutcomes[9].bufferedComplete).toBe(false);
    expect(buffered?.quantityOutcomes[9].bufferedReturnPercent).toBeNull();
    expect(buffered?.quantityOutcomes.slice(0, 10).every((point) => !point.budgetBest)).toBe(true);
    expect(buffered?.quantityOutcomes[20].bufferedComplete).toBe(true);
    expect(buffered?.quantityOutcomes[20].bufferedFinalUnits).toBe(23);
    expect(buffered?.quantityOutcomes[20].actionable).toBe(true);
  });

  it("never describes an unplaceable zero-output leg as a total loss", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 3),
      pair(chaos, exalted, 10, 1),
    ]);
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Kulemak's Invitation", wantAmount: 3, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 10 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: "Exalted Orb", wantAmount: 4, haveAmount: 1 },
        rates,
      ),
    ];
    const point = analyzeLoops("kulemaks-invitation", seen, rates, {
      safetyBufferBps: 0,
      executionConcessionBps: 0,
    }).bestVerifiedLoop?.quantityOutcomes[0];

    expect(point?.nominalComplete).toBe(false);
    expect(point?.nominalBlockedStep).toBe(1);
    expect(point?.nominalBlockedUnits).toBe(3);
    expect(point?.nominalFinalUnits).toBe(0);
    expect(point?.nominalReturnPercent).toBeNull();
    expect(point?.actionable).toBe(false);
  });

  it("preserves whole-unit and buffer invariants across adversarial rate combinations", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 1),
      pair(chaos, exalted, 1, 1),
    ]);
    let seed = 0x5eed1234;
    const randomRate = () => {
      seed = (Math.imul(seed, 1_664_525) + 1_013_904_223) >>> 0;
      return (5 + seed % 19_996) / 100;
    };

    for (let trial = 0; trial < 200; trial += 1) {
      const legRates = [randomRate(), randomRate(), randomRate()];
      const seen = [
        resolveObservation(
          {
            wantText: "Chaos Orb",
            haveText: "Kulemak's Invitation",
            wantAmount: legRates[0],
            haveAmount: 1,
          },
          rates,
        ),
        resolveObservation(
          {
            wantText: "Exalted Orb",
            haveText: "Chaos Orb",
            wantAmount: legRates[1],
            haveAmount: 1,
          },
          rates,
        ),
        resolveObservation(
          {
            wantText: "Kulemak's Invitation",
            haveText: "Exalted Orb",
            wantAmount: legRates[2],
            haveAmount: 1,
          },
          rates,
        ),
      ];
      const result = analyzeLoops("kulemaks-invitation", seen, rates, {
        safetyBufferBps: 750,
        executionConcessionBps: 0,
        minPercent: 0,
      });
      const loop = result.bestVerifiedLoop;
      expect(loop).toBeDefined();
      expect(
        (loop?.bufferedMultiplier ?? 0) / (loop?.nominalMultiplier ?? 1),
      ).toBeCloseTo(0.925, 12);

      for (const point of loop?.quantityOutcomes ?? []) {
        let nominal = point.quantity;
        let nominalComplete = true;
        for (const rate of legRates) {
          nominal = Number(
            BigInt(nominal) * BigInt(Math.round(rate * 100)) / 100n,
          );
          if (nominal === 0) {
            nominalComplete = false;
            break;
          }
        }
        expect(point.nominalComplete).toBe(nominalComplete);
        expect(point.nominalReturnPercent === null).toBe(!nominalComplete);
        if (nominalComplete) expect(point.nominalFinalUnits).toBe(nominal);
        if (point.bufferedComplete) {
          expect(point.bufferedFinalUnits).toBeLessThanOrEqual(point.nominalFinalUnits);
          expect(point.bufferedReturnPercent).not.toBeNull();
        } else {
          expect(point.bufferedReturnPercent).toBeNull();
          expect(point.actionable).toBe(false);
        }
      }
    }
  });

  it("models a faster-fill concession independently on every directed leg", () => {
    const result = analyzeLoops("omen-whittling", observations(), book(), {
      now: 10_500,
      executionConcessionBps: 500,
      safetyBufferBps: 500,
    });
    const loop = result.loops[0];

    expect(result.executionConcessionBps).toBe(500);
    expect(result.executionConcessionLoopPercent).toBeCloseTo(14.2625, 8);
    expect(loop.executionMultiplier / loop.nominalMultiplier).toBeCloseTo(
      0.95 ** 3,
      12,
    );
    expect(loop.bufferedMultiplier / loop.executionMultiplier).toBeCloseTo(
      0.95,
      12,
    );
    for (const leg of loop.legs) {
      expect(leg.executionRate).toBeCloseTo(leg.rate * 0.95, 12);
    }
  });

  it("scores quantities and notches from the faster-fill safety outcome", () => {
    const rates = buildRateBook([
      pair(kulemak, chaos, 1, 3),
      pair(chaos, exalted, 1, 1),
    ]);
    const seen = [
      resolveObservation(
        { wantText: "Chaos Orb", haveText: "Kulemak's Invitation", wantAmount: 3, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Exalted Orb", haveText: "Chaos Orb", wantAmount: 1, haveAmount: 1 },
        rates,
      ),
      resolveObservation(
        { wantText: "Kulemak's Invitation", haveText: "Exalted Orb", wantAmount: 0.5, haveAmount: 1 },
        rates,
      ),
    ];
    const loop = analyzeLoops("kulemaks-invitation", seen, rates, {
      executionConcessionBps: 500,
      safetyBufferBps: 500,
      minPercent: 0,
    }).bestVerifiedLoop;

    expect(loop).toBeDefined();
    for (const point of loop?.quantityOutcomes ?? []) {
      if (point.executionComplete) {
        expect(point.executionFinalUnits).toBeLessThanOrEqual(point.nominalFinalUnits);
      }
      if (point.bufferedComplete) {
        expect(point.bufferedFinalUnits).toBeLessThanOrEqual(point.executionFinalUnits);
        expect(point.actionable).toBe(
          (point.bufferedReturnPercent ?? -Infinity) >= 0,
        );
      } else {
        expect(point.actionable).toBe(false);
      }
    }
    const ranked = (loop?.quantityOutcomes ?? []).filter(
      (point) => point.localPeak || point.budgetBest,
    );
    expect(ranked.length).toBeGreaterThan(0);
    expect(ranked.every((point) => point.bufferedComplete)).toBe(true);
  });
});
