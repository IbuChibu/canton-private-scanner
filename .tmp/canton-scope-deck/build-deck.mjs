import { Presentation, PresentationFile } from "@oai/artifact-tool";
import { buildSlide01 } from "./slide-01.mjs";
import { buildSlide05 } from "./slide-05.mjs";
import { buildSlide17 } from "./slide-17.mjs";
import { buildSlide19 } from "./slide-19.mjs";

const OUTPUT = "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/Canton_Scope_Project_Overview.pptx";
const BLACK = "#000000";
const BLUE = "#3D8DFF";
const MUTED = "#4B5563";
const FONT = "Helvetica Neue";

function textBlock(text, fontSize = 24, options = {}) {
  return {
    runs: [{
      run: text,
      textStyle: {
        fontSize: `${fontSize}px`,
        typeface: FONT,
        color: options.color ?? BLACK,
        bold: options.bold ?? false,
      },
    }],
    spaceAfter: options.spaceAfter ?? 0,
    paragraphStyle: {
      lineSpacingPercent: options.lineSpacingPercent ?? 108000,
    },
  };
}

function richBlock(runs, options = {}) {
  return {
    runs: runs.map((item) => ({
      run: item.text,
      textStyle: {
        fontSize: `${item.size ?? 22}px`,
        typeface: FONT,
        color: item.color ?? BLACK,
        bold: item.bold ?? false,
      },
    })),
    spaceAfter: options.spaceAfter ?? 0,
    paragraphStyle: {
      lineSpacingPercent: options.lineSpacingPercent ?? 112000,
    },
  };
}

function twoColumnBody(title, lines) {
  return {
    titleHere: textBlock(title, 30, { bold: true, spaceAfter: 1300 }),
    loremIpsumDolorSitAmetConsecteturAdipiscing: richBlock(
      lines.flatMap((line, index) => [
        { text: `${line.lead}\n`, size: 22, bold: true, color: BLUE },
        { text: `${line.body}${index === lines.length - 1 ? "" : "\n\n"}`, size: 21, color: MUTED },
      ]),
      { lineSpacingPercent: 108000 },
    ),
  };
}

function setNotes(slide, narrative, sources) {
  slide.speakerNotes.textFrame.setText(
    `${narrative}\n\n[Sources]\n${sources.map((source) => `- ${source}`).join("\n")}\n[/Sources]`,
  );
}

const presentation = Presentation.create({
  slideSize: { width: 1280, height: 720 },
});

const slide1 = buildSlide01(presentation, {
  title: textBlock("CANTOR8  •  A1 SCANNER", 24, { bold: true, color: BLUE }),
  title2: richBlock([
    { text: "Canton Scope\n", size: 80, bold: true },
    { text: "Private ledger. Made visible.", size: 72, bold: true },
  ], { lineSpacingPercent: 90000 }),
  title3: textBlock(
    "A resumable, rights-aware index for balances and transfer history.",
    28,
    { color: MUTED, lineSpacingPercent: 105000 },
  ),
});
slide1.background.fill = "#FFFFFF";
setNotes(
  slide1,
  "Introduce Canton Scope as a private ledger scanner, not a global block explorer. The product turns the data an authenticated Canton user may see into a durable local index and browser dashboard.",
  [
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/README.md",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/PROJECT_PLAN.md",
  ],
);

const slide2 = buildSlide05(presentation, {
  title: textBlock("Privacy is the constraint—and the design", 39, { bold: true }),
  body1: twoColumnBody("What Canton exposes", [
    { lead: "Party-scoped disclosure", body: "A node sees only data its hosted or authorized parties may observe." },
    { lead: "No query for “everything”", body: "A conventional network-wide block explorer would violate the ledger’s privacy model." },
  ]),
  body2: twoColumnBody("What the scanner does", [
    { lead: "Rights-aware discovery", body: "It reads user rights, caches local readable parties, and never probes Holdings across the directory." },
    { lead: "Explicit subscriptions", body: "Every live request uses filtersByParty for the active selection—never filtersForAnyParty." },
  ]),
  footer1: "2",
});
slide2.background.fill = "#FFFFFF";
setNotes(
  slide2,
  "Frame privacy as the reason the project exists. Canton Scope only indexes an authenticated user’s authorized view, and selection cannot bypass Canton permissions.",
  [
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/README.md",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/scanner.py",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/updates.py",
  ],
);

const timelineBody = (title, body) => ({
  titleHere: textBlock(title, 27, { bold: true, spaceAfter: 900 }),
  loremIpsumDolorSitAmetConsecteturAdipiscing: textBlock(body, 20, {
    color: MUTED,
    lineSpacingPercent: 108000,
  }),
});

const slide3 = buildSlide17(presentation, {
  title: textBlock("Correctness comes from doing things in the right order", 39, { bold: true }),
  label1: textBlock("01  SNAPSHOT", 20, { bold: true, color: BLUE }),
  label2: textBlock("02  STREAM", 20, { bold: true, color: BLUE }),
  label3: textBlock("03  COMMIT", 20, { bold: true, color: BLUE }),
  body1: timelineBody("Exact-offset ACS", "Read the ledger end once, then query the Holding interface for each selected party at that same offset."),
  body2: timelineBody("Party-scoped updates", "Open the transaction-tree stream from beginExclusive at the saved checkpoint and walk every child event."),
  body3: timelineBody("Atomic SQLite state", "Holdings, private events, semantic transfers, and the new offset commit together—or all roll back."),
  footer1: "3",
});
slide3.background.fill = "#FFFFFF";
setNotes(
  slide3,
  "Explain the trap the scanner avoids: streaming only from the current ledger end would miss today’s balances. The exact ACS offset becomes the exclusive starting point for live updates, and transactional persistence makes replay safe.",
  [
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/scanner.py",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/updates.py",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/database.py",
  ],
);

const slide4 = buildSlide05(presentation, {
  title: textBlock("The dashboard turns a private index into an operator view", 39, { bold: true }),
  body1: twoColumnBody("Scanner operations", [
    { lead: "Live health", body: "Connection state, heartbeat, persisted offset, and active versus desired party counts." },
    { lead: "Controlled selection", body: "Searchable cached parties, protected mutation, and automatic reconciliation by the managed worker." },
  ]),
  body2: twoColumnBody("Ledger experience", [
    { lead: "Exact balances", body: "Decimal values are rendered without floating-point conversion, grouped by instrument." },
    { lead: "Conservative history", body: "Only confirmed semantic transfers appear; inactive parties keep historical activity without a current balance." },
  ]),
  footer1: "4",
});
slide4.background.fill = "#FFFFFF";
setNotes(
  slide4,
  "Show how the FastAPI and plain JavaScript frontend make the index useful. The browser receives scanner data, not Canton credentials; the optional admin token stays only in tab memory.",
  [
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/api.py",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/frontend/app.js",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/DEMO_RUNBOOK.md",
  ],
);

const slide5 = buildSlide19(presentation, {
  title: textBlock("Demo-ready: live, resumable, and local", 39, { bold: true }),
  body1: {
    topic: textBlock("VALIDATED ON DEVNET", 20, { bold: true, color: BLUE, spaceAfter: 900 }),
    loremIpsumDolorSitAmetConsecteturAdipiscing: textBlock(
      "The service restarted from its persisted checkpoint, reconnected without rereading the ACS, and advanced the live index.",
      22,
      { color: MUTED },
    ),
  },
  stat1: textBlock("3", 68, { bold: true, color: BLUE }),
  stat2: textBlock("2,278+", 60, { bold: true, color: BLUE }),
  stat3: textBlock("49", 68, { bold: true, color: BLUE }),
  body2: textBlock("rights-verified\nactive parties", 22, { bold: true }),
  body3: textBlock("offsets advanced\nafter resume", 22, { bold: true }),
  body4: textBlock("automated checks\npassed", 22, { bold: true }),
  footer1: "5",
});
slide5.background.fill = "#FFFFFF";
setNotes(
  slide5,
  "Close on evidence. The live run resumed from offset 2,920,767 and reached 2,923,045. Forty-one Python tests and eight JavaScript tests passed. The result is a private, restart-safe scanner suitable for the local hackathon demo.",
  [
    "Live /health validation recorded 2026-08-29",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/tests/test_scanner.py",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/tests/test_frontend_status.mjs",
    "/Users/ebrahimakhoon/Documents/cantor-hackathon-toolkit/DEMO_RUNBOOK.md",
  ],
);

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(OUTPUT);
