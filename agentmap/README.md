# agentmap: milestone 2

Passive CDP capture (Page, DOM, DOMSnapshot, Accessibility, Emulation only; never Runtime, no script injection) turned into a
budgeted, foldable outline with stable IDs.

## Commands
    export NO_PROXY=127.0.0.1,localhost
    python3 agentmap.py map <url> <name> [--budget 3000] [--dpr 1]   -> out/<name>.map.txt .png .stats.json .snap.json .cap.json.gz
    python3 agentmap.py expand <name> <region_id> [--budget 3000]     -> one region in full (all collapsed items, full text)
    python3 agentmap.py crop <name> <id> [out.png]                    -> PNG of a control or region
    python3 agentmap.py diff <nameA> <nameB>                          -> ID-keyed change report
    python3 agentmap.py mapfile <file.html> <name>                    -> same as map, for local fixtures
expand/crop work offline from the saved .cap.json.gz, so the agent loop can call them without re-loading the page.

## What M2 added
a. Labels: unnamed lists take the nearest preceding sibling text/heading/link (<=40 chars), else the nearest short text
   directly above them (<=90px, x-overlap, same landmark), else the landmark's own name. SarkariResult columns now read
   list "Result", "Admit Card", "Latest Job", "Answer Key", "Syllabus", ...
b. Folding: heading runs become sections; every region/section/table/long paragraph starts folded as a one-line summary
   (`▸ [r:id] § Title — 416 words, 66 links`). A heap unfolds regions by value-per-token (controls > structure > prose,
   Main and dialogs first) until the budget is hit; a final pass guarantees the map ends <= budget.
c. expand(region_id): `r:` regions and `p:` paragraphs; lists show every item, text is untruncated, nested regions
   unfold within their own budget.
d. crop(id): the viewport is grown to the page height before reading boxes (no captureBeyondViewport relayout, so boxes
   and pixels agree). DOMSnapshot bounds are device px when DSF != 1; the unit is measured (contentWidth / CSS width) and
   normalised to CSS px, then multiplied by the measured DPR (image px / clip CSS px). Verified pixel-exact at DPR 1 and 2.
e. diff: snapshots key controls by ID, list regions by ID (item count, first items), paragraphs by opening words.
   Controls whose region suffix changed are paired as "moved" instead of add+remove; long text shows word-level +/-.

## Stable IDs
- controls `lnk:apply-online`, made unique by region scope `lnk:apply-online@latest-job`, then a counter
- regions `r:<label>`, paragraphs `p:<first five words>`
- synthetic sections (from headings, which ads can inject) never scope control IDs

## Known issues / next
- geometric labels can borrow a nearby unrelated string (Amazon's category strip reads "0 items in cart")
- unlabelled controls show "(no label)": next step is DOM attributes (href, placeholder, title) via DOM.describeNode
- full-page shot caps at 16,000 device px; crop() of a node below that raises a clear error (Wikipedia at DPR 2)
- IRCTC: still Akamai "Access Denied" from a datacenter IP (needs residential egress or the real-browser extension)
