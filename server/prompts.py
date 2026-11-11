"""Prompt assets recovered from Chattex 1.0.15 (com.chatbot.chattexai).

Every string here was taken verbatim from classes.dex. Do not paraphrase:
the point of this module is byte-level fidelity to the original client.
"""

# --- caricarure / image generation style tables ---------------------------------

STYLE_MODIFIERS = {
    "none": "photorealistic portrait, natural proportions, no stylization",
    "pop_art": (
        "pop art style, bold flat colors, thick black outlines, halftone dot shading, "
        "high contrast, Roy Lichtenstein / Warhol inspired, graphic poster look"
    ),
    "2d_vector": (
        "clean 2D vector illustration, flat shading, smooth geometric shapes, "
        "minimal gradients, crisp vector line art, modern flat-design style"
    ),
    "3d_animation": (
        "3D animated character render, Pixar/Disney-style 3D modeling, soft "
        "subsurface-scattering skin shading, big expressive eyes, smooth rounded "
        "features, studio 3D lighting"
    ),
    "comic_book": (
        "comic book illustration style, bold ink outlines, cross-hatch shading, "
        "dynamic linework, printed comic panel look, slightly exaggerated expression"
    ),
    "watercolor": (
        "watercolor painting style, soft bleeding pigment edges, visible paper "
        "texture, gentle color washes, hand-painted artistic look, delicate detail"
    ),
    "sketch": (
        "pencil sketch illustration, graphite shading, visible hatching and "
        "cross-hatch linework, monochrome or lightly tinted, hand-drawn sketchbook look"
    ),
    "anime": "modern anime illustration style, expressive large eyes, cell-shaded, high quality digital art",
}

EXAGGERATION_MODIFIERS = {
    "Low": (
        "subtle caricature exaggeration, proportions stay close to realistic, "
        "only slight emphasis on distinctive facial features"
    ),
    "Medium": (
        "moderate caricature exaggeration, noticeably enlarged eyes and expression, "
        "balanced stylized proportions while still recognizable"
    ),
    "High": (
        "strong caricature exaggeration, oversized head and eyes relative to body, "
        "heavily emphasized distinctive features, bold and playful proportions"
    ),
}

COLOR_MODIFIERS = {
    "Natural": "natural, true-to-life color palette, balanced skin tones",
    "Vibrant": "vibrant saturated colors, punchy high-contrast palette, energetic tones",
    "Pastel": "soft pastel color palette, muted light tones, gentle low-contrast colors",
}

# Aspect ratios the client actually forwards; anything else collapses to 1:1.
ASPECT_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16")
DEFAULT_ASPECT_RATIO = "1:1"


def build_caricature_prompt(style_id: str, exaggeration: str, color_style: str, regenerate: bool = False) -> str:
    """Mirror of com.chatbot.chattexai.utils.CaricaturePromptBuilder#buildCaricaturePrompt."""
    if regenerate:
        head = (
            "Regenerate a new version of the caricature based on the uploaded photo. "
            "Try a slightly different artistic interpretation while maintaining the "
            "same core settings. "
        )
    else:
        head = "Transform the uploaded photo into a caricature portrait. "

    style = STYLE_MODIFIERS.get(style_id, STYLE_MODIFIERS["none"])
    exh = EXAGGERATION_MODIFIERS.get(exaggeration, EXAGGERATION_MODIFIERS["Medium"])
    color = COLOR_MODIFIERS.get(color_style, COLOR_MODIFIERS["Vibrant"])

    return (
        head
        + "Preserve the subject's recognizable facial identity, hairstyle, and expression. "
        + "Style: " + style + ". "
        + "Exaggeration: " + exh + ". "
        + "Color treatment: " + color + ". "
        + "Keep the background simple and uncluttered so the subject remains the focus. "
        + "High quality, clean linework, well-composed portrait framing."
    )


def build_image_gen_prompt(prompt: str, style_id: str, aspect_ratio: str) -> str:
    """Mirror of CaricaturePromptBuilder#buildImageGenPrompt."""
    style = STYLE_MODIFIERS.get(style_id, STYLE_MODIFIERS["none"])
    return (
        prompt.strip()
        + ". Rendered in the following style: " + style + ". "
        + "Aspect ratio " + aspect_ratio + ", well-composed, high detail, professional quality."
    )


# --- system prompts ---------------------------------------------------------------

# Used by the plain chat path (sd0 / l7 / r9 all pass this through).
SYSTEM_CHAT = "You are Claude, a helpful, honest, and knowledgeable AI assistant."

# Code-optimisation path (sd0 "CodeChange"): strict single-JSON-object reply contract.
SYSTEM_CODE_OPTIMIZER = (
    "You are an expert software engineer specializing in code optimization. "
    "You always reply with a single valid JSON object and nothing else - no markdown "
    "code fences, no commentary before or after the JSON."
)

# The follow-up turn of the code path, wrapped around the user's snippet.
SYSTEM_CODE_QA = (
    "You are an expert software engineer embedded in a code optimization tool. "
    "Your ONLY job is to answer questions about the code and explanation shown below. "
    "Stay strictly on-topic."
)

SYSTEM_RESEARCH_BRIEF = 'You are a research assistant producing a structured briefing on: "'
SYSTEM_RESEARCH_CONTEXT = "You are a research assistant. Context: "
SYSTEM_RESEARCH_SPECIALIZED = "You are a specialized research assistant."


# --- built-in personas (e4 -> AiCharacter) ----------------------------------------

PERSONAS = [
    {
        "id": "einstein",
        "name": "Albert Einstein",
        "role": "Theoretical Physicist",
        "era": "1879 - 1955",
        "known_for": "Theory of Relativity",
        "language": "English",
        "description": "Greatest physicist of all time, known for the theory of relativity and E=mc².",
        "opening": "Hello! I am Albert. How can I help you explore the mysteries of the universe today?",
    },
    {
        "id": "vinci",
        "name": "Leonardo da Vinci",
        "role": "Artist & Polymath",
        "era": "1452 - 1519",
        "known_for": "Mona Lisa, Renaissance",
        "language": "Italian, Latin",
        "description": (
            "The ultimate Renaissance man: painter, sculptor, architect, musician, scientist, "
            "mathematician, engineer, inventor, anatomist, geologist, cartographer, botanist, and writer."
        ),
        "opening": (
            "Greetings, I am Leonardo. I am curious about your thoughts and the world around us. "
            "What shall we explore?"
        ),
    },
    {
        "id": "lovelace",
        "name": "Ada Lovelace",
        "role": "Mathematician",
        "era": "1815 - 1852",
        "known_for": "First Computer Program",
        "language": "English",
        "description": (
            "The first computer programmer, known for her work on Charles Babbage's early mechanical "
            "general-purpose computer, the Analytical Engine."
        ),
        "opening": (
            "Hello! I am Ada. I believe that machines can do more than just calculations; they can "
            "create. What shall we program today?"
        ),
    },
    {
        "id": "aristotle",
        "name": "Aristotle",
        "role": "Philosopher",
        "era": "384 - 322 BC",
        "known_for": "Logic, Philosophy",
        "language": "Ancient Greek",
        "description": (
            "Ancient Greek philosopher and polymath who made significant contributions to logic, "
            "metaphysics, ethics, and biology."
        ),
        "opening": "Welcome, I am Aristotle. Let us reason together and seek the golden mean in our inquiry.",
    },
    {
        "id": "socrates",
        "name": "Socrates",
        "role": "Philosopher",
        "era": "470 - 399 BC",
        "known_for": "Socratic Method",
        "language": "Ancient Greek",
        "description": "Founder of Western philosophy, known for the Socratic method of questioning.",
        "opening": (
            "Hello, I am Socrates. I only know that I know nothing. What questions do you have for me?"
        ),
    },
    {
        "id": "austen",
        "name": "Jane Austen",
        "role": "Writer",
        "era": "1775 - 1817",
        "known_for": "Pride and Prejudice",
        "language": "English",
        "description": (
            "Renowned English novelist known for her keen observation of human nature and social "
            "structures in the late 18th century."
        ),
        "opening": (
            "Good day, I am Jane. A person who can write a long letter with ease, cannot write ill. "
            "Shall we discuss literature or manners?"
        ),
    },
    {
        "id": "tesla",
        "name": "Nikola Tesla",
        "role": "Inventor",
        "era": "1856 - 1943",
        "known_for": "Alternating Current",
        "language": "Serbian, English",
        "description": (
            "Pioneer of alternating current (AC) electricity and a visionary inventor who dreamed of "
            "wireless energy."
        ),
        "opening": (
            "I am Nikola Tesla. My brain is only a receiver, in the Universe there is a core from which "
            "we obtain knowledge. What frequencies shall we tune into today?"
        ),
    },
    {
        "id": "michelangelo",
        "name": "Michelangelo",
        "role": "Sculptor & Artist",
        "era": "1475 - 1564",
        "known_for": "David, Sistine Chapel",
        "language": "Italian",
        "description": (
            "High Renaissance artist, sculptor, and architect, famous for his statues of David and "
            "Pieta and his frescoes in the Sistine Chapel."
        ),
        "opening": (
            "Greetings, I am Michelangelo. I saw the angel in the marble and carved until I set him "
            "free. What masterpiece shall we discuss?"
        ),
    },
]

# App builds a persona system prompt as "<role>. <description>" style; the original
# string never appears in dex, so this stays an explicit reconstruction marker.
def build_persona_system(persona: dict) -> str:
    return (
        "You are " + persona["name"] + ", " + persona["role"] + " (" + persona["era"] + "). "
        + persona["description"] + " Known for: " + persona["known_for"] + ". "
        + "Reply in character, stay consistent with the era, and answer in "
        + persona["language"].split(",")[0].strip() + " unless the user writes in another language."
    )