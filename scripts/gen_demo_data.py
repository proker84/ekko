"""Genera i dataset demo (deterministici, seed fisso) per la modalità senza API key."""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(42)
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
DATA = Path(__file__).resolve().parents[1] / "data"
DATA.mkdir(exist_ok=True)

POS = [
    "Personale gentile e disponibile, esperienza ottima.",
    "Ottimo prodotto, qualità eccellente. Consigliato!",
    "Consegna puntuale e pacco perfetto, corriere impeccabile.",
    "Rapporto qualità prezzo davvero conveniente.",
    "Locale pulito e curato, staff cordiale.",
    "Assistenza rapida, hanno risolto il reso in un giorno.",
    "Prenotazione facilissima e nessuna attesa.",
]
NEG = [
    "Tempi lunghi, ho dovuto aspettare più di un'ora. Servizio lento.",
    "Prodotto difettoso e qualità scadente, deluso.",
    "Consegna in ritardo di una settimana, corriere irreperibile.",
    "Prezzo troppo caro per quello che offre.",
    "Personale scortese al telefono, esperienza pessima.",
    "Assistenza inesistente: nessuna risposta alla richiesta di rimborso.",
    "Locale sporco, igiene da rivedere.",
]
MID = [
    "Nella media, prezzo ok ma attesa migliorabile.",
    "Buon prodotto ma spedizione lenta.",
    "Staff gentile, però la prenotazione online non funzionava.",
]
REPLIES = [
    "Grazie per il feedback! Ci fa molto piacere.",
    "Ci scusiamo per il disagio: la contattiamo in privato per risolvere.",
    "Grazie della segnalazione, stiamo migliorando proprio su questo punto.",
]
SUGGESTIONS = [
    "Sarebbe utile poter prenotare online anche la sera.",
    "Dovreste aggiungere personale nel weekend per ridurre l'attesa.",
    "Manca un parcheggio: potreste convenzionarvi con il garage vicino.",
    "Consiglio di inviare la conferma dell'appuntamento via WhatsApp.",
    "Potreste ampliare le opzioni vegetariane del menu.",
    "Sarebbe meglio avere un corriere alternativo per le consegne.",
    "Suggerisco di aggiornare il sito, la prenotazione online è lenta.",
    "Perché non introdurre una tessera fedeltà per i clienti abituali?",
]

def make(n, source, trend_bias):
    out = []
    for i in range(n):
        age = int(random.triangular(0, 720, 200))
        published = NOW - timedelta(days=age, hours=random.randint(0, 23))
        # trend_bias > 0: recensioni recenti migliori (trend in crescita)
        recent = age < 90
        p_good = 0.55 + (trend_bias if recent else 0)
        r = random.random()
        if r < p_good:
            stars, text = random.choice([4, 5]), random.choice(POS)
        elif r < p_good + 0.25:
            stars, text = 3, random.choice(MID)
        else:
            stars, text = random.choice([1, 2]), random.choice(NEG)
        # una parte degli utenti aggiunge un suggerimento esplicito
        p_sug = 0.10 if stars >= 4 else 0.30
        if random.random() < p_sug:
            text = f"{text} {random.choice(SUGGESTIONS)}"
        replied = random.random() < (0.5 if stars <= 2 else 0.25)
        out.append({
            "id": f"{source}{i:04d}",
            "author": f"user_{source}_{i}",
            "stars": stars,
            "text": text,
            "lang": "it",
            "published_at": published.isoformat(),
            "reply": random.choice(REPLIES) if replied else None,
        })
    return out

(DATA / "demo_reviews_google.json").write_text(
    json.dumps(make(85, "g", 0.15), indent=2, ensure_ascii=False))
(DATA / "demo_reviews_trustpilot.json").write_text(
    json.dumps(make(45, "t", 0.10), indent=2, ensure_ascii=False))
print("demo data generati in", DATA)
