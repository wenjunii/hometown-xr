"""
Multilingual keyword dictionary for the fast pre-filter stage.

Keywords are organized by language code. Each language has seed terms
covering concept clusters: hometown, childhood, belonging, roots,
diaspora, nostalgia, home/homeland.

The pre-filter is intentionally broad — false positives are cleaned up
by the semantic similarity stage.
"""

# fmt: off
KEYWORDS_BY_LANGUAGE = {
    # ── English ──────────────────────────────────────────────────────────
    "en": [
        "hometown", "home town", "birthplace", "native town", "home village",
        "ancestral home", "place of origin", "where I grew up", "where I come from",
        "homeland", "motherland", "fatherland", "native land", "home country",
        "childhood home", "childhood memories", "growing up", "upbringing",
        "sense of belonging", "rootedness", "where I belong",
        "my roots", "heritage", "ancestry", "cultural roots", "ancestral",
        "nostalgia", "homesick", "homesickness", "homecoming", "return home",
        "diaspora", "displacement", "uprooted", "exile",
        "going back home", "back to my roots",
    ],

    # ── Chinese (Simplified + Traditional) ───────────────────────────────
    "zh": [
        "故乡", "家乡", "老家", "乡愁", "故土", "祖国", "母国",
        "童年", "儿时", "小时候", "长大的地方",
        "归属感", "根", "根源", "寻根",
        "思乡", "落叶归根", "衣锦还乡",
        "家园", "原籍", "出生地", "家鄉", "鄉愁", "故鄉",
        "童年回憶", "歸屬感",
    ],

    # ── Spanish ──────────────────────────────────────────────────────────
    "es": [
        "pueblo natal", "tierra natal", "ciudad natal", "lugar de origen",
        "donde crecí", "patria", "madre patria",
        "infancia", "niñez", "recuerdos de infancia",
        "pertenencia", "arraigo", "sentido de pertenencia",
        "raíces", "herencia", "ancestros", "orígenes",
        "nostalgia", "añoranza", "morriña",
        "diáspora", "desarraigo", "exilio",
        "volver a casa", "mi hogar",
    ],

    # ── French ───────────────────────────────────────────────────────────
    "fr": [
        "ville natale", "pays natal", "lieu de naissance", "terre natale",
        "là où j'ai grandi", "patrie", "mère patrie",
        "enfance", "souvenirs d'enfance",
        "appartenance", "sentiment d'appartenance", "enracinement",
        "racines", "origines", "héritage", "ancêtres",
        "nostalgie", "mal du pays",
        "diaspora", "déracinement", "exil",
        "retour au pays", "chez moi", "mon foyer",
    ],

    # ── German ───────────────────────────────────────────────────────────
    "de": [
        "Heimat", "Heimatstadt", "Heimatort", "Heimatdorf", "Geburtsort",
        "Geburtsstadt", "Vaterland", "Mutterland",
        "wo ich aufgewachsen bin", "wo ich herkomme",
        "Kindheit", "Kindheitserinnerungen",
        "Zugehörigkeit", "Zugehörigkeitsgefühl", "Verwurzelung",
        "Wurzeln", "Herkunft", "Abstammung", "Ahnen",
        "Sehnsucht", "Heimweh", "Nostalgie",
        "Diaspora", "Entwurzelung", "Exil",
        "Zuhause", "nach Hause", "Heimkehr",
    ],

    # ── Japanese ─────────────────────────────────────────────────────────
    "ja": [
        "故郷", "ふるさと", "地元", "出身地", "生まれ故郷",
        "母国", "祖国",
        "子供時代", "幼少期", "幼い頃", "育った場所",
        "帰属", "所属感", "帰属意識",
        "ルーツ", "根", "先祖", "起源",
        "望郷", "懐かしい", "郷愁", "ホームシック",
        "帰郷", "里帰り",
    ],

    # ── Korean ───────────────────────────────────────────────────────────
    "ko": [
        "고향", "출생지", "태어난 곳", "자란 곳",
        "모국", "조국",
        "어린 시절", "어릴 때", "유년기",
        "소속감", "귀속감",
        "뿌리", "기원", "조상", "유산",
        "향수", "그리움", "향수병",
        "귀향", "고향으로", "디아스포라",
    ],

    # ── Arabic ───────────────────────────────────────────────────────────
    "ar": [
        "مسقط رأس", "وطن", "بلد الأصل", "مسقط الرأس",
        "أرض الوطن", "الوطن الأم",
        "طفولة", "ذكريات الطفولة", "نشأت",
        "انتماء", "الشعور بالانتماء",
        "جذور", "أصول", "تراث", "أسلاف",
        "حنين", "الحنين إلى الوطن", "غربة",
        "شتات", "منفى", "اغتراب",
        "العودة إلى الوطن",
    ],

    # ── Portuguese ────────────────────────────────────────────────────────
    "pt": [
        "terra natal", "cidade natal", "lugar de origem",
        "onde cresci", "pátria", "mãe pátria",
        "infância", "memórias de infância",
        "pertencimento", "senso de pertencimento", "enraizamento",
        "raízes", "origens", "herança", "ancestrais",
        "nostalgia", "saudade", "saudades de casa",
        "diáspora", "desenraizamento", "exílio",
        "voltar para casa", "meu lar",
    ],

    # ── Russian ──────────────────────────────────────────────────────────
    "ru": [
        "родной город", "родина", "малая родина", "место рождения",
        "отчизна", "отечество",
        "детство", "воспоминания детства", "где я вырос",
        "принадлежность", "чувство принадлежности",
        "корни", "истоки", "наследие", "предки", "происхождение",
        "ностальгия", "тоска по дому", "тоска по родине",
        "диаспора", "изгнание", "эмиграция",
        "возвращение домой", "домой",
    ],

    # ── Hindi ────────────────────────────────────────────────────────────
    "hi": [
        "जन्मभूमि", "घर", "गांव", "देश", "मातृभूमि",
        "जहाँ मैं बड़ा हुआ", "पैतृक गाँव",
        "बचपन", "बचपन की यादें",
        "अपनापन", "जुड़ाव",
        "जड़ें", "विरासत", "पूर्वज", "मूल",
        "पुरानी यादें", "घर की याद",
        "प्रवासी", "विस्थापन",
    ],

    # ── Turkish ──────────────────────────────────────────────────────────
    "tr": [
        "memleket", "doğduğum yer", "ana yurt", "yurt",
        "büyüdüğüm yer", "vatan", "anavatan",
        "çocukluk", "çocukluk anıları",
        "aidiyet", "aidiyet duygusu", "ait olmak",
        "kökler", "köken", "miras", "atalar",
        "nostalji", "özlem", "hasret", "gurbet",
        "diaspora", "sürgün", "göç",
        "eve dönüş", "memleketim",
    ],

    # ── Italian ──────────────────────────────────────────────────────────
    "it": [
        "paese natale", "città natale", "luogo di nascita", "terra d'origine",
        "dove sono cresciuto", "patria", "madrepatria",
        "infanzia", "ricordi d'infanzia",
        "appartenenza", "senso di appartenenza", "radicamento",
        "radici", "origini", "eredità", "antenati",
        "nostalgia", "mal di casa",
        "diaspora", "sradicamento", "esilio",
        "tornare a casa", "casa mia",
    ],

    # ── Vietnamese ───────────────────────────────────────────────────────
    "vi": [
        "quê hương", "quê nhà", "nơi sinh", "đất mẹ",
        "nơi tôi lớn lên", "tổ quốc",
        "tuổi thơ", "kỷ niệm tuổi thơ",
        "sự thuộc về", "cảm giác thuộc về",
        "cội nguồn", "nguồn gốc", "di sản", "tổ tiên",
        "hoài niệm", "nhớ nhà", "nỗi nhớ quê",
        "kiều bào", "lưu vong",
        "trở về quê", "về nhà",
    ],

    # ── Thai ─────────────────────────────────────────────────────────────
    "th": [
        "บ้านเกิด", "ภูมิลำเนา", "แผ่นดินแม่",
        "ที่ที่ฉันเติบโต", "มาตุภูมิ",
        "วัยเด็ก", "ความทรงจำวัยเด็ก",
        "ความเป็นส่วนหนึ่ง", "การเป็นส่วนหนึ่ง",
        "รากเหง้า", "ต้นกำเนิด", "มรดก", "บรรพบุรุษ",
        "ความคิดถึง", "คิดถึงบ้าน",
        "พลัดถิ่น", "ลี้ภัย",
        "กลับบ้าน",
    ],

    # ── Indonesian / Malay ───────────────────────────────────────────────
    "id": [
        "kampung halaman", "tanah kelahiran", "tempat asal",
        "tempat saya dibesarkan", "tanah air", "ibu pertiwi",
        "masa kecil", "kenangan masa kecil",
        "rasa memiliki", "rasa kepunyaan",
        "akar", "asal usul", "warisan", "leluhur",
        "nostalgia", "rindu kampung", "rindu rumah",
        "diaspora", "pengasingan",
        "pulang kampung", "rumah saya",
    ],

    # ── Polish ───────────────────────────────────────────────────────────
    "pl": [
        "rodzinne miasto", "miejsce urodzenia", "ojczyzna",
        "gdzie dorastałem", "mała ojczyzna",
        "dzieciństwo", "wspomnienia z dzieciństwa",
        "przynależność", "poczucie przynależności", "zakorzenienie",
        "korzenie", "pochodzenie", "dziedzictwo", "przodkowie",
        "nostalgia", "tęsknota za domem",
        "diaspora", "wygnanie", "emigracja",
        "powrót do domu",
    ],

    # ── Dutch ────────────────────────────────────────────────────────────
    "nl": [
        "geboorteplaats", "geboortedorp", "geboortestad", "vaderland",
        "waar ik ben opgegroeid", "moederland",
        "jeugd", "kindertijd", "jeugdherinneringen",
        "verbondenheid", "gevoel van thuishoren", "saamhorigheid",
        "wortels", "afkomst", "erfgoed", "voorouders",
        "heimwee", "nostalgie",
        "diaspora", "ballingschap",
        "thuiskomen", "naar huis",
    ],

    # ── Swedish ──────────────────────────────────────────────────────────
    "sv": [
        "hemstad", "födelseort", "hemby", "fosterland",
        "där jag växte upp", "moderland",
        "barndom", "barndomsminnen",
        "tillhörighet", "känsla av tillhörighet",
        "rötter", "ursprung", "arv", "förfäder",
        "hemlängtan", "nostalgi",
        "diaspora", "exil",
        "hemkomst", "hem",
    ],

    # ── Ukrainian ────────────────────────────────────────────────────────
    "uk": [
        "рідне місто", "батьківщина", "мала батьківщина", "місце народження",
        "вітчизна",
        "дитинство", "спогади дитинства", "де я виріс",
        "належність", "відчуття належності",
        "коріння", "витоки", "спадщина", "предки", "походження",
        "ностальгія", "туга за домом",
        "діаспора", "вигнання", "еміграція",
        "повернення додому",
    ],
}
# fmt: on

# Universal fallback keywords — broad terms likely to appear in any language
# These are romanized or widely used across scripts
UNIVERSAL_KEYWORDS = [
    "hometown", "homeland", "motherland", "diaspora", "nostalgia",
]


def get_keywords(language_code: str) -> list[str]:
    """
    Get keywords for a given language code.
    Falls back to universal keywords if the language is not explicitly covered.
    Also always includes universal keywords for broader coverage.
    """
    lang_keywords = KEYWORDS_BY_LANGUAGE.get(language_code, [])
    return lang_keywords + UNIVERSAL_KEYWORDS


def get_all_keywords_flat() -> set[str]:
    """
    Get all keywords across all languages as a flat set.
    Used by the keyword pre-filter which doesn't know the language yet.
    """
    all_kw = set()
    for keywords in KEYWORDS_BY_LANGUAGE.values():
        for kw in keywords:
            all_kw.add(kw.lower())
    for kw in UNIVERSAL_KEYWORDS:
        all_kw.add(kw.lower())
    return all_kw
