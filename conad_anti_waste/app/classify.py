"""Rule-based product classification — no AI required.

Maps a raw Conad product name to (category, storage zone, shelf life, is_food).
Conad names are unusually structured ("Yogurt greco Neogal 0% grassi conf. 150 g
x 2"), so ordered keyword matching gets very high coverage. The table below was
tuned against the 160 distinct products in the real order history.

ORDER MATTERS. Rules are evaluated top to bottom, first match wins, so the
specific cases sit above the generic ones. Several real products depend on this:

    "Olive all'Ascolana Surgelate"  -> surgelati, not olive in salamoia
    "Plumcake con Yogurt Magro"     -> dolci, not yogurt
    "Coppa Cacao con panna montata" -> dessert, not coppa (salume)
    "Parboiled insalate kg 1"       -> riso, not insalata in busta
    "Lievito di birra"              -> lievito, not birra
    "Passata di Pomodoro"           -> conserva, not pomodoro fresco
    "Insalata Russa"                -> gastronomia, not insalata in busta

When the LLM arrives it re-classifies only rows tagged `classified_by='rules'`,
never a `user` correction.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

FRIGO = "frigo"
FREEZER = "freezer"
DISPENSA = "dispensa"
NON_FOOD = "non_food"

UNKNOWN_CATEGORY = "sconosciuto"
# Anything unmatched fails safe: treated as fresh and short-lived, so it surfaces
# in the expiry view instead of silently rotting in a corner of the database.
UNKNOWN_DEFAULT = (UNKNOWN_CATEGORY, FRIGO, 7, True)


@dataclass(frozen=True)
class Classification:
    category: str
    storage_zone: str
    shelf_life_days: Optional[int]
    is_food: bool
    matched: bool  # False => fell through to the default, needs human review


# (regex, category, zone, shelf_life_days)  — is_food is False iff zone is non_food
RULES: list[tuple[str, str, str, Optional[int]]] = [
    # --- non-food: must win over any food keyword they happen to contain -----
    (r"asciugatutto|carta igienica|maxi rotoli|\brotoli\b", "carta casa", NON_FOOD, None),
    (r"lavastoviglie|detersiv|ace pavimenti|igienizzante|candeggina|sgrassat",
     "detersivi", NON_FOOD, None),
    (r"alcool|bagnoschiuma|listerine|dentifric|shampoo|sapone|palmolive",
     "igiene e casa", NON_FOOD, None),

    # --- freezer: the word "surgelat" outranks whatever the product is ------
    (r"surgelat|\bgelato\b", "surgelati", FREEZER, 180),

    # --- desserts & sweets (before salumi: "Coppa Cacao"; before yogurt) -----
    (r"plumcake|crostatin|\bwafer\b|biscott|gocciole|digestive|frollin",
     "dolci confezionati", DISPENSA, 180),
    (r"crema alle nocciole|nutella|marmellat|confettur", "creme spalmabili", DISPENSA, 365),
    (r"profiteroles|panna cotta|bunet|budino|danette|coppa cacao|panna montata|tiramis",
     "dessert freschi", FRIGO, 20),
    (r"cheerios|cereali|kinder colazione|corn flakes", "cereali", DISPENSA, 240),
    # Fruit pouches, before fresh produce — "Mela, Pesca e Mango 100 g" is a
    # shelf-stable snack, not fruit that belongs in the fridge for 21 days.
    (r"mela e fragola|mela, fragola|mela, pesca|frullat|omogeneizzat",
     "frutta frullata", DISPENSA, 365),

    # --- drinks (before fruit: "Thè Limone", "Bevanda alla Pesca") ----------
    (r"lievito", "lievito", DISPENSA, 365),  # "Lievito di birra" must precede birra
    (r"birrificio|\bbirra\b|\bipa\b|luppoli", "birra", DISPENSA, 365),
    (r"coca-cola|chinotto|aranciata|\bcola\b|gassosa", "bibite", DISPENSA, 365),
    (r"bevanda|succo|\bsucco e polpa\b|pfanner|\bace e mela\b|nettare",
     "succhi", DISPENSA, 365),
    (r"tisane|\bthe\b|\btea\b|camomill|infuso", "the e tisane", DISPENSA, 730),
    (r"barista|latte di mandorla|bevanda vegetale|avena 1 l", "bevande vegetali", DISPENSA, 365),
    (r"acqua minerale|san benedetto|\bnaturale 1,5", "acqua", DISPENSA, 730),

    # --- tins, jars, sauces -------------------------------------------------
    # This whole block sits ABOVE the pantry staples on purpose: "Tonno all'Olio
    # di Oliva" and "Sardine all'Olio di Oliva" would otherwise be classified as
    # olive oil and given a 540-day pantry life.
    (r"\btonno\b|sardin|sgombro|alici|acciugh|salmone in scatola",
     "conserve di pesce", DISPENSA, 1095),
    (r"\bceci\b|piselli|fagiol|lenticch|mais in scatola",
     "legumi in scatola", DISPENSA, 1095),
    (r"passata|pelati|polpa di pomodoro|concentrato di pomodoro",
     "conserve di pomodoro", DISPENSA, 730),
    (r"amatriciana|arrabbiata|\bragu\b|pesto|sugo|salsa di|basilico 400",
     "sughi pronti", DISPENSA, 730),
    (r"ketchup|maionese|guacamole|senape|salsa ", "salse", DISPENSA, 365),
    (r"olive .*salamoia|\bolive\b|capperi|sottaceti|sottoli",
     "olive e sottaceti", DISPENSA, 730),

    # --- bakery -------------------------------------------------------------
    (r"grissini|schiacciatine|cracker|taralli|salatini(?!.*surgelat)",
     "snack salati", DISPENSA, 120),
    (r"pan carre|pancarre|finette|pane in cassetta|fette biscottate",
     "pane confezionato", DISPENSA, 25),
    (r"piadina|tortilla|\bwrap\b", "piadine", DISPENSA, 60),
    (r"\bpinsa\b|\bpane\b|focaccia|baguette|ciabatta", "pane fresco", DISPENSA, 5),

    # --- pantry staples (after conserves and bakery — see the note above) ----
    (r"pasta di semola|pasta di grano|spaghett|fusill|penne|farfalle|conchiglie|"
     r"sedani rigati|pipette|bavettine|rigatoni|mezze maniche",
     "pasta secca", DISPENSA, 730),
    (r"parboiled|\bribe\b|\briso\b|basmati|carnaroli|arborio", "riso", DISPENSA, 730),
    (r"farina di grano|farina tipo|\bfarina\b|semolino", "farina", DISPENSA, 365),
    (r"sale alimentare|sale grosso|sale fino|sale iodato", "sale", DISPENSA, 3650),
    (r"olio extra vergine|olio di oliva|olio di semi", "olio", DISPENSA, 540),
    (r"aceto", "aceto", DISPENSA, 1825),
    (r"gelatina in fogli|colla di pesce|amido|maizena", "addensanti", DISPENSA, 730),
    (r"pure di patate|puree di patate|fiocchi di patate", "purè", DISPENSA, 365),
    (r"\bbrodo\b|dado da cucina", "brodo", DISPENSA, 365),
    (r"risotto alla|preparato per risotto", "preparati", DISPENSA, 365),
    (r"zucchero|cacao amaro|vanillina", "dispensa dolce", DISPENSA, 730),

    # --- fresh pasta & ready meals (before salumi: they name their filling) --
    (r"tortellin|cappellett|mezzelune|ravioli|agnolott|tortelli",
     "pasta fresca ripiena", FRIGO, 30),
    (r"gnocchi", "gnocchi", DISPENSA, 90),
    (r"tortino|insalata russa|polpett|cotolett|lasagn",
     "gastronomia", FRIGO, 20),

    # --- cheese & dairy -----------------------------------------------------
    (r"mozzarell|burrata|fiordilatte|perline di mozzarella", "mozzarella", FRIGO, 12),
    (r"crescenza|stracchino|robiola|philadelphia|spalmabile|nuvola|ricotta|mascarpone",
     "formaggi freschi", FRIGO, 12),
    (r"grana padano|parmigiano|pecorino|grattugiat|stagionatura minima 24",
     "formaggi stagionati", FRIGO, 90),
    (r"\bfeta\b|halloumi|babybel|emmental|gorgonzola|provolone|scamorza|asiago|cipro dop",
     "formaggi", FRIGO, 40),
    (r"\bburro\b|margarina", "burro", FRIGO, 60),
    (r"yogurt|\bkefir\b", "yogurt", FRIGO, 28),
    (r"\buova\b|\buovo\b", "uova", FRIGO, 21),
    (r"panna da cucina|latte fresco|latte intero|latte parzialmente", "latte e panna", FRIGO, 8),

    # --- cured meats & deli -------------------------------------------------
    (r"wurstel|hot dog", "würstel", FRIGO, 30),
    (r"prosciutto|mortadella|pancetta|\bcoppa\b|salame|speck|bresaola|"
     r"dolce crudo|cotto cubetti|salumeria",
     "salumi", FRIGO, 15),
    (r"tartare|scottona|macinato|hamburger|\bbistecc|\bfilett[oi] di (manzo|maiale)",
     "carne fresca", FRIGO, 3),
    (r"arrosto|petto di tacchino|alette di pollo|\bpollo\b|\btacchino\b",
     "gastronomia carne", FRIGO, 4),

    # --- fresh produce (after drinks & conserves) ---------------------------
    (r"insalata|lattuga|spinacin|rucola|valeriana .*insalat|misticanza|\bdelizia\b",
     "insalata in busta", FRIGO, 5),
    (r"pomodoro|pomodori(?!.* pelati)|datterin|ciliegia a grappolo|cuore di bue",
     "pomodori freschi", FRIGO, 8),
    (r"cetrioli|zucchin|melanzan|peperon|carote|broccol|cavolo|finocch|sedano",
     "verdura fresca", FRIGO, 10),
    (r"banane|\bbanana\b", "banane", FRIGO, 6),
    (r"limon|arance|\bmele\b|\bmela\b(?!.*fragola)|pere\b", "agrumi e mele", FRIGO, 21),
    (r"melone|angur|ananas", "melone", FRIGO, 7),
    (r"\bpesca\b|pesche|albicocc|susine|nettarin", "frutta estiva", FRIGO, 6),
    (r"\buva\b", "uva", FRIGO, 8),
    (r"patate(?!.*fiocchi)|cipoll|aglio", "patate e cipolle", DISPENSA, 30),
]

_COMPILED = [(re.compile(p), c, z, d) for p, c, z, d in RULES]

_UNIT_RE = re.compile(
    r"(?:(\d+)\s*x\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g(?![a-z])|ml|cl|\bl\b)", re.IGNORECASE
)


def fold(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace — the matching form.

    Accents are stripped so rules can be written in plain ASCII: "Ragù" folds to
    "ragu", "Purè" to "pure", "Thè" to "the".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def normalize_name(name: str) -> str:
    """Identity key for a product across orders.

    Conad varies capitalisation between orders ("... CONAD" vs "... Conad"), so
    folding alone already merges most duplicates.
    """
    folded = fold(name)
    folded = re.sub(r"[^\w\s,%°'\-]", " ", folded)
    return " ".join(folded.split())


def parse_unit_size(name: str) -> Optional[str]:
    """Extract a human-readable pack size: '3 x 125 g', '1,5 l', '500 g'."""
    m = _UNIT_RE.search(fold(name))
    if not m:
        return None
    multi, value, unit = m.groups()
    unit = unit.strip()
    return f"{multi} x {value} {unit}" if multi else f"{value} {unit}"


def classify(name: str) -> Classification:
    folded = fold(name)
    for pattern, category, zone, days in _COMPILED:
        if pattern.search(folded):
            return Classification(
                category=category,
                storage_zone=zone,
                shelf_life_days=days,
                is_food=zone != NON_FOOD,
                matched=True,
            )
    category, zone, days, is_food = UNKNOWN_DEFAULT
    return Classification(category, zone, days, is_food, matched=False)
