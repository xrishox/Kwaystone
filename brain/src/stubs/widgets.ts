// Types copied VERBATIM from Exiled-Exchange-2
// renderer/src/web/overlay/widgets.ts (Widget, Anchor, PriceCheckWidget).
// Kept byte-faithful so upstream re-pulls can diff cleanly.

export interface Widget {
  wmId: number;
  wmType: string;
  wmTitle: string;
  wmWants: "show" | "hide";
  wmZorder: number | "exclusive" | null;
  wmFlags: Array<WellKnownFlag | string>;
}

export type WellKnownFlag =
  | "uninitialized"
  | "menu::skip"
  | "has-browser"
  | "invisible-on-blur"
  | "hide-on-blur"
  | "hide-on-focus"
  | "ignore-ui-visibility";

export interface Anchor {
  pos: string;
  x: number;
  y: number;
}

export interface PriceCheckWidget extends Widget {
  hotkey: string | null;
  hotkeyHold: string;
  hotkeyLocked: string | null;
  showSeller: false | "account" | "ign";
  searchStatRange: number;
  showRateLimitState: boolean;
  apiLatencySeconds: number;
  collapseListings: "api" | "app";
  smartInitialSearch: boolean;
  lockedInitialSearch: boolean;
  activateStockFilter: boolean;
  showCursor: boolean;
  requestPricePrediction: boolean;
  builtinBrowser: boolean;
  rememberCurrency: boolean;
  defaultAllSelected: boolean;
  itemHoverTooltip: "off" | "keybind" | "always";
  autoFillEmptyRuneSockets: "Iron Rune" | false;
  alwaysShowTier: boolean;
  openItemEditorAbove: boolean;
  coreCurrency: "exalted" | "chaos";
  currencyVolume: "none" | "value" | "item" | "both";
  rememberListingType: boolean;
}

export const PRICE_CHECK_DEFAULTS: Partial<PriceCheckWidget> = {
  showSeller: "account", searchStatRange: 10, apiLatencySeconds: 2,
  collapseListings: "api", smartInitialSearch: true, lockedInitialSearch: true,
  activateStockFilter: false, requestPricePrediction: false,
  rememberCurrency: false, defaultAllSelected: false,
  autoFillEmptyRuneSockets: false, alwaysShowTier: false,
  coreCurrency: "exalted", currencyVolume: "none", rememberListingType: false,
};
