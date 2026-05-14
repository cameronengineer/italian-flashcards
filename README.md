# Italian flashcard generation pipeline

## 1. Project overview

The project is a data-processing and AI-assisted content generation system for creating language-learning flashcard decks.

The first implementation focuses on Italian, but the system should be designed so that the same architecture can later support other languages.

The system starts with a frequency dictionary: a list of words ordered from most frequently used to least frequently used. Each word is processed individually. Depending on the type of word, the system determines what kind of language-learning content should be generated.

For Italian verbs, the system identifies the infinitive form, generates conjugations across key tenses and grammatical persons, and then converts those outputs into flashcard-ready data.

---

## 2. High-level goal

Create a repeatable pipeline that can:

1. Take a frequency-ordered word list.
2. Process each word individually.
3. Identify the word type, such as verb, noun, adjective, adverb, preposition, etc.
4. Apply the appropriate generation workflow for that word type.
5. For verbs, identify the infinitive form.
6. Generate useful conjugation data.
7. Convert the generated language data into structured flashcard decks.
8. Export the final decks for use in Anki or another spaced-repetition system.

---

## 3. Functional diagram

```mermaid
flowchart TD
    A[Frequency dictionary<br/>Ordered word list] --> B[Select next word]
    B --> C[Word analysis stage]

    C --> D{What type of word is it?}

    D -->|Verb: infinitive| E[Use word as infinitive]
    D -->|Verb: conjugated form| F[AI infinitive extraction]
    D -->|Non-verb| G[Route to other word-type workflows]

    F --> H[Validated infinitive form]
    E --> H

    H --> I[AI conjugation generation]

    I --> J[Generate tense/person grid]

    J --> K[Presente]
    J --> L[Passato prossimo]
    J --> M[Presente progressivo]
    J --> N[Imperativo]
    J --> O[Imperfetto]
    J --> P[Futuro semplice]
    J --> Q[Condizionale presente]

    K --> R[Six grammatical persons]
    L --> R
    M --> R
    N --> R
    O --> R
    P --> R
    Q --> R

    R --> S[Validation and quality checks]
    S --> T[Flashcard generation]
    T --> U[Deck formatting]
    U --> V[Export to Anki-compatible format]

    G --> W[Future workflows:<br/>nouns, adjectives, prepositions, pronouns, etc.]
```

---

## 4. Verb-processing workflow

### 4.1 Input

The input is a single Italian word from the frequency dictionary.

Examples:

* essere
* sono
* avere
* ha
* andare
* vado
* fatto
* mangiare
* mangiato

---

### 4.2 Word classification

The system determines whether the word is a verb.

Possible outcomes:

| Input type      | Example | Action                                                               |
| --------------- | ------: | -------------------------------------------------------------------- |
| Infinitive verb |  andare | Use directly as the infinitive                                       |
| Conjugated verb |    vado | Identify the infinitive: andare                                      |
| Past participle |   fatto | Identify likely infinitive: fare                                     |
| Ambiguous form  |    sono | Resolve likely infinitive: essere, based on context or lexical rules |
| Non-verb        |    casa | Send to non-verb workflow                                            |

---

## 5. Infinitive extraction

If the word is a verb but not an infinitive, the system runs an AI process to identify the infinitive.

### Example

Input word:

> vado

AI output:

> andare

Another example:

Input word:

> ho

AI output:

> avere

Another example:

Input word:

> era

AI output:

> essere

---

## 6. Conjugation generation

Once the infinitive has been identified, the system generates conjugations across selected tenses and grammatical persons.

### Target tenses

The first version should generate the following Italian verb forms:

1. Presente
2. Passato prossimo
3. Presente progressivo
4. Imperativo
5. Imperfetto
6. Futuro semplice
7. Condizionale presente

---

## 7. Grammatical persons

For each tense, the system generates forms for the six standard grammatical persons.

| Person                 | Italian pronoun | English equivalent |
| ---------------------- | --------------- | ------------------ |
| First person singular  | io              | I                  |
| Second person singular | tu              | you                |
| Third person singular  | lui / lei       | he / she / it      |
| First person plural    | noi             | we                 |
| Second person plural   | voi             | you plural         |
| Third person plural    | loro            | they               |

---

## 8. Example output for one verb

For the infinitive:

> parlare

The system would generate a structured output similar to this.

| Tense                 | io           | tu            | lui/lei      | noi             | voi            | loro            |
| --------------------- | ------------ | ------------- | ------------ | --------------- | -------------- | --------------- |
| Presente              | parlo        | parli         | parla        | parliamo        | parlate        | parlano         |
| Passato prossimo      | ho parlato   | hai parlato   | ha parlato   | abbiamo parlato | avete parlato  | hanno parlato   |
| Presente progressivo  | sto parlando | stai parlando | sta parlando | stiamo parlando | state parlando | stanno parlando |
| Imperfetto            | parlavo      | parlavi       | parlava      | parlavamo       | parlavate      | parlavano       |
| Futuro semplice       | parlerò      | parlerai      | parlerà      | parleremo       | parlerete      | parleranno      |
| Condizionale presente | parlerei     | parleresti    | parlerebbe   | parleremmo      | parlereste     | parlerebbero    |

The imperativo is slightly different because it does not naturally use all six persons in the same way as indicative tenses. A practical version could include:

| Person | Imperativo |
| ------ | ---------- |
| tu     | parla      |
| Lei    | parli      |
| noi    | parliamo   |
| voi    | parlate    |
| loro   | parlino    |

For the first version of the system, the imperative may need a special handling rule rather than being treated as a standard six-person tense.

---

## 9. Flashcard generation logic

Once the conjugation table is generated, the system converts each item into one or more flashcards.

### Possible card types

#### Recognition card

Front:

> parlavo

Back:

> I was speaking / I used to speak
> Infinitive: parlare
> Tense: imperfetto
> Person: io

#### Production card

Front:

> How do you say “I was speaking” in Italian?

Back:

> parlavo

#### Infinitive recognition card

Front:

> vado

Back:

> Infinitive: andare
> Meaning: I go / I am going

#### Conjugation prompt card

Front:

> Conjugate parlare in the presente for io.

Back:

> parlo

---

## 10. Recommended data structure

A structured JSON-style output for each verb could look like this:

```json
{
  "source_word": "vado",
  "word_type": "verb",
  "infinitive": "andare",
  "language": "Italian",
  "conjugations": {
    "presente": {
      "io": "vado",
      "tu": "vai",
      "lui_lei": "va",
      "noi": "andiamo",
      "voi": "andate",
      "loro": "vanno"
    },
    "passato_prossimo": {
      "io": "sono andato/andata",
      "tu": "sei andato/andata",
      "lui_lei": "è andato/andata",
      "noi": "siamo andati/andate",
      "voi": "siete andati/andate",
      "loro": "sono andati/andate"
    }
  }
}
```

This structure keeps the system flexible and allows the same source data to be converted into different flashcard formats later.

---

## 11. Quality control requirements

Because AI-generated conjugations can contain errors, the pipeline should include a validation stage.

Recommended validation checks:

1. Confirm the infinitive exists in a dictionary or lexical database.
2. Confirm the tense labels are valid for the target language.
3. Confirm the generated conjugations match a trusted conjugation source where possible.
4. Flag irregular verbs for extra review.
5. Flag ambiguous source words.
6. Flag forms where gender or number affects the answer, such as passato prossimo with essere.
7. Flag defective or unusual verbs.
8. Detect duplicate cards before export.

---

## 12. Special Italian issues to handle

Italian verb generation needs some language-specific rules.

### 12.1 Auxiliary verbs in passato prossimo

Some verbs use avere:

> ho parlato

Some verbs use essere:

> sono andato / sono andata

The system needs to store which auxiliary is used.

### 12.2 Gender and number agreement

With essere verbs, the past participle changes by gender and number:

| Subject             | Example      |
| ------------------- | ------------ |
| io masculine        | sono andato  |
| io feminine         | sono andata  |
| noi masculine/mixed | siamo andati |
| noi feminine        | siamo andate |

The flashcard system needs to decide whether to:

1. Show both masculine and feminine forms.
2. Default to masculine/mixed forms.
3. Generate separate cards for masculine and feminine answers.

### 12.3 Imperativo special handling

Imperativo should not be treated like a normal six-person tense.

Unlike presente, imperfetto, futuro semplice, and condizionale presente, the imperative is used for commands, instructions, requests, warnings, and prohibitions. It also has different behaviour in positive and negative forms.

The system should therefore handle imperativo as a specialised verb workflow.

---

#### 12.3.1 Core principle

For imperativo, the system should only generate cards for forms that are useful, irregular, or meaningfully different from standard predictable patterns.

The goal is not to create unnecessary cards for every possible imperative form. Instead, the system should prioritise forms that a learner is likely to misunderstand, forget, or need in real speech.

---

#### 12.3.2 Imperative persons to include

Italian imperative does not normally use first-person singular.

The useful imperative persons are:

| Person | Use                         | Example with parlare |
| ------ | --------------------------- | -------------------- |
| tu     | informal singular command   | parla                |
| Lei    | formal singular command     | parli                |
| noi    | let’s...                    | parliamo             |
| voi    | informal plural command     | parlate              |
| loro   | formal plural command, rare | parlino              |

The system should usually generate cards for:

1. tu
2. Lei
3. noi
4. voi

The loro imperative should be optional or lower priority because it is rare in everyday use.

---

#### 12.3.3 Positive imperative

Positive imperative forms should be generated where they are useful or deviate from simple expectations.

Examples:

| Infinitive | tu        | Lei    | noi       | voi      |
| ---------- | --------- | ------ | --------- | -------- |
| parlare    | parla     | parli  | parliamo  | parlate  |
| prendere   | prendi    | prenda | prendiamo | prendete |
| dormire    | dormi     | dorma  | dormiamo  | dormite  |
| andare     | vai / va’ | vada   | andiamo   | andate   |
| fare       | fai / fa’ | faccia | facciamo  | fate     |
| essere     | sii       | sia    | siamo     | siate    |
| avere      | abbi      | abbia  | abbiamo   | abbiate  |

Recommended card rule:

* Generate positive imperative cards for irregular verbs.
* Generate positive imperative cards for high-frequency verbs.
* For regular verbs, generate only enough examples to teach the pattern, unless the deck goal is exhaustive generation.

---

#### 12.3.4 Negative imperative

Negative imperative needs separate handling.

For informal tu commands, the negative imperative is usually:

> non + infinitive

Examples:

| Positive tu imperative | Negative tu imperative |
| ---------------------- | ---------------------- |
| parla                  | non parlare            |
| prendere               | non prendere           |
| dormi                  | non dormire            |
| andare                 | non andare             |
| essere                 | non essere             |
| fare                   | non fare               |

Example sentence:

> Non essere difficile.
> Don’t be difficult.

For other persons, the negative imperative generally uses:

> non + imperative/subjunctive-style form

Examples with parlare:

| Person | Negative imperative |
| ------ | ------------------- |
| tu     | non parlare         |
| Lei    | non parli           |
| noi    | non parliamo        |
| voi    | non parlate         |
| loro   | non parlino         |

The system should therefore generate negative imperative separately from positive imperative.

---

#### 12.3.5 Imperative with pronouns

Imperatives often combine with pronouns. These forms are common and useful, but they may create a large number of cards.

Examples:

| Base command | With pronoun | Meaning          |
| ------------ | ------------ | ---------------- |
| dimmi        | di’ + mi     | tell me          |
| dammi        | da’ + mi     | give me          |
| fammi        | fa’ + mi     | let me / make me |
| ascoltami    | ascolta + mi | listen to me     |
| guardalo     | guarda + lo  | look at it/him   |
| prendilo     | prendi + lo  | take it          |
| aiutami      | aiuta + mi   | help me          |

Special doubled-consonant forms occur with some short irregular imperatives:

| Verb   | Imperative + pronoun | Meaning           |
| ------ | -------------------- | ----------------- |
| dire   | dimmi                | tell me           |
| dare   | dammi                | give me           |
| fare   | fammi                | let me / make me  |
| stare  | stammi               | stay / be with me |
| andare | vacci                | go there          |

Recommended card rule:

* Include pronoun-attached imperatives only for high-frequency, high-value phrases.
* Treat them as phrase cards rather than full-table conjugation cards.
* Prioritise common chunks like dimmi, dammi, fammi, aiutami, ascoltami, guardalo, prendilo, and vieni.

---

#### 12.3.6 Negative imperative with pronouns

Negative imperatives can place pronouns either before or after the verb in some cases, especially with tu.

Examples:

| Form           | Meaning       |
| -------------- | ------------- |
| non mi dire    | don’t tell me |
| non dirmi      | don’t tell me |
| non lo fare    | don’t do it   |
| non farlo      | don’t do it   |
| non mi aiutare | don’t help me |
| non aiutarmi   | don’t help me |

Recommended card rule:

* For the first version, generate only the simpler, more transparent form.
* Prefer non + infinitive + attached pronoun for tu forms where appropriate, such as non farlo.
* Add alternate accepted forms as notes rather than separate cards unless they are common.

---

#### 12.3.7 Imperative card types

Imperativo should generate different card types from ordinary tense cards.

Recommended cards:

| Card type            | Front                                      | Back         |
| -------------------- | ------------------------------------------ | ------------ |
| Positive command     | Tell one person informally: “Speak!”       | Parla!       |
| Negative command     | Tell one person informally: “Don’t speak!” | Non parlare! |
| Formal command       | Tell someone formally: “Speak!”            | Parli!       |
| Let’s form           | Say: “Let’s speak.”                        | Parliamo.    |
| Irregular command    | Informal command: “Be good.”               | Sii bravo/a. |
| Phrase card          | “Tell me!”                                 | Dimmi!       |
| Negative phrase card | “Don’t do it!”                             | Non farlo!   |

---

#### 12.3.8 Imperativo generation rules

Recommended system rules:

1. Do not generate an io imperative card.
2. Generate tu, Lei, noi, and voi forms.
3. Make loro optional or advanced.
4. Generate negative imperative forms separately.
5. For regular verbs, generate only high-frequency or pattern-teaching examples.
6. For irregular verbs, generate cards more aggressively.
7. For high-frequency verbs, generate both positive and negative imperatives.
8. For pronoun-attached forms, generate only common phrase-based cards.
9. Flag irregular imperative forms for review.
10. Store imperative cards separately from normal tense/person cards.

---

#### 12.3.9 Suggested data structure for imperativo

```json
{
  "infinitive": "essere",
  "imperativo": {
    "positive": {
      "tu": "sii",
      "Lei": "sia",
      "noi": "siamo",
      "voi": "siate",
      "loro": "siano"
    },
    "negative": {
      "tu": "non essere",
      "Lei": "non sia",
      "noi": "non siamo",
      "voi": "non siate",
      "loro": "non siano"
    },
    "noteworthy_forms": [
      "sii",
      "non essere",
      "sia",
      "siate"
    ],
    "phrase_cards": [
      {
        "front": "Don't be difficult.",
        "back": "Non essere difficile."
      },
      {
        "front": "Be good.",
        "back": "Sii bravo/a."
      }
    ]
  }
}
```

---

#### 12.3.10 Recommended MVP handling for imperativo

For the minimum viable version, imperativo should generate:

1. Positive tu form, only if irregular or high-frequency.
2. Negative tu form.
3. Formal Lei form, if common or useful.
4. Voi form, if high-frequency or classroom/useful instruction language.
5. Common phrase cards for irregular imperatives.

Examples of high-value imperative verbs:

* essere: sii, non essere
* avere: abbi, non avere
* fare: fa’ / fai, non fare, fammi, non farlo
* dire: di’, dimmi, non dire, non dirmi
* dare: da’ / dai, dammi, non dare
* andare: va’ / vai, vai, non andare, vacci
* stare: sta’ / stai, stai, non stare
* venire: vieni, non venire
* guardare: guarda, non guardare
* ascoltare: ascolta, non ascoltare
* prendere: prendi, non prendere
* lasciare: lascia, non lasciare

### 12.4 Ambiguous word forms

Some Italian forms can map to multiple possible infinitives or grammatical functions.

Example:

> sono

Possible interpretations:

* essere, io sono
* essere, loro sono

The system should either:

1. Store all valid interpretations.
2. Choose the most likely interpretation based on frequency.
3. Create multiple cards.
4. Flag the item for manual review.

---

## 13. Proposed pipeline stages

### Stage 1: Input preparation

* Import frequency dictionary.
* Clean data.
* Remove duplicates.
* Normalise casing.
* Store rank and frequency metadata.

### Stage 2: Word analysis

* Identify word type.
* Detect whether word is a verb.
* Detect whether verb is infinitive or conjugated.

### Stage 3: Infinitive resolution

* For infinitive verbs, pass through unchanged.
* For conjugated verbs, identify infinitive.
* Store confidence score.
* Flag ambiguity.

### Stage 4: Verb generation

* Generate conjugations for target tenses.
* Generate six-person conjugation tables where applicable.
* Apply Italian-specific rules for auxiliary verbs, agreement, and imperatives.

### Stage 5: Validation

* Check against trusted grammar/conjugation sources.
* Validate tense/person completeness.
* Flag irregular or ambiguous outputs.

### Stage 6: Flashcard transformation

* Convert conjugation data into flashcard rows.
* Generate front text.
* Generate back text.
* Add metadata fields.
* Add tags.

### Stage 7: Export

* Export to CSV, TSV, JSON, or Anki package format.
* Preserve metadata so cards can be filtered by frequency, tense, verb type, difficulty, or irregularity.

---

## 14. Flashcard structure and rendering rules

Each generated flashcard has two sides:

1. Front side
2. Back side

The system should support bidirectional cards. For every Italian-to-English card, there should be a matching English-to-Italian card.

---

### 14.1 Core card behaviour

The flashcard should behave as a reveal card, not a full replacement card.

When the card first appears, it shows the front content.

When the learner checks or flips the card, the back content appears underneath the original front content. The original front content remains visible.

This means the learner sees:

1. The prompt content first.
2. Then the answer content added below it.
3. Any image remains visible after the card is flipped.

---

### 14.2 Italian-to-English card layout

For Italian-to-English cards, the front side shows:

* Italian word or phrase
* Image, where available
* Audio track that plays when the card appears
* Small labels, where relevant

After the card is flipped, the back side appears underneath and shows:

* English meaning
* Additional labels or grammar information, where relevant

Example:

#### Front before reveal

Italian:

> parliamo

Audio:

> Plays automatically when card appears

Labels:

> presente
> noi
> verb

#### After reveal

Italian:

> parliamo

English:

> we speak / we are speaking

Labels:

> presente
> noi
> verb

---

### 14.3 English-to-Italian card layout

For English-to-Italian cards, the front side shows:

* English word or phrase
* Image, where available
* Small labels, where relevant

After the card is flipped, the back side appears underneath and shows:

* Italian word or phrase
* Audio track
* Additional labels or grammar information, where relevant

Example:

#### Front before reveal

English:

> we speak / we are speaking

Labels:

> presente
> noi
> verb

#### After reveal

English:

> we speak / we are speaking

Italian:

> parliamo

Audio:

> Available or played on reveal

Labels:

> presente
> noi
> verb

---

### 14.4 Persistent image behaviour

Images should remain visible on both sides of the card.

The image should not disappear when the card is flipped.

The image can be used for:

* nouns
* concrete verbs
* adjectives
* phrase cards
* common objects
* places
* actions

For abstract words, the image field may be empty or replaced with an icon/visual cue if appropriate.

---

### 14.5 Audio behaviour

For Italian-to-English cards:

* Audio should play when the card first appears.
* The audio should correspond to the Italian word or phrase shown on the front.

For English-to-Italian cards:

* Audio may play after reveal, because the learner should attempt to recall the Italian before hearing it.
* The audio should correspond to the Italian answer.

This prevents the audio from giving away the answer on English-to-Italian production cards.

---

### 14.6 Labels on cards

Cards should support small visible labels.

Labels help the learner understand what kind of card they are looking at without needing long explanations.

Labels may include:

* word type
* tense
* grammatical person
* gender
* number
* article type
* preposition
* register
* irregularity
* frequency band
* deck category

Examples:

| Card content | Possible labels                             |
| ------------ | ------------------------------------------- |
| parliamo     | verb, presente, noi                         |
| parlavo      | verb, imperfetto, io                        |
| non parlare  | verb, imperativo, negative, tu              |
| il cane      | noun, masculine, singular, definite article |
| i cani       | noun, masculine, plural, definite article   |
| alla casa    | noun, feminine, singular, a + la            |
| delle case   | noun, feminine, plural, di + le             |

---

### 14.7 Verb card labels

Verb cards should include labels for:

1. Word type
2. Tense or mood
3. Person
4. Number, where useful
5. Polarity, for negative forms
6. Register, where useful
7. Irregularity, where useful

Example:

For:

> parliamo

Labels:

* verb
* presente
* noi

For:

> non essere

Labels:

* verb
* imperativo
* negative
* tu

For:

> sia

Labels:

* verb
* imperativo
* formal
* Lei
* irregular

---

### 14.8 Noun card labels

Noun cards should include labels for:

1. Word type
2. Gender
3. Number
4. Article type
5. Preposition, where applicable
6. Irregularity, where useful

Example:

For:

> il cane

Labels:

* noun
* masculine
* singular
* definite article

For:

> i cani

Labels:

* noun
* masculine
* plural
* definite article

For:

> alla casa

Labels:

* noun
* feminine
* singular
* articulated preposition
* a + la

---

### 14.9 Bidirectional card generation

Every generated flashcard should have a matching reverse-direction card.

That means each language item produces at least two cards:

1. Italian to English
2. English to Italian

Example pair:

| Direction          | Front                      | Back                       |
| ------------------ | -------------------------- | -------------------------- |
| Italian to English | parliamo                   | we speak / we are speaking |
| English to Italian | we speak / we are speaking | parliamo                   |

The reverse card should preserve relevant metadata, labels, images, and audio settings.

However, audio behaviour may differ by direction:

| Direction          | Audio behaviour                                                          |
| ------------------ | ------------------------------------------------------------------------ |
| Italian to English | Play Italian audio on card appearance                                    |
| English to Italian | Do not play Italian audio before reveal; play or show audio after reveal |

---

### 14.10 One-to-one card mapping

The system should maintain a one-to-one relationship between forward and reverse cards.

Each generated card should have:

* a unique card ID
* a pair ID linking it to its reverse card
* a direction field
* shared source metadata

Example:

```json
{
  "card_id": "it-verb-parlare-presente-noi-forward",
  "pair_id": "it-verb-parlare-presente-noi",
  "direction": "italian_to_english",
  "front": {
    "text": "parliamo",
    "language": "Italian",
    "audio": "parliamo.mp3",
    "image": null,
    "labels": ["verb", "presente", "noi"]
  },
  "back": {
    "text": "we speak / we are speaking",
    "language": "English",
    "labels": ["verb", "presente", "noi"]
  }
}
```

Matching reverse card:

```json
{
  "card_id": "it-verb-parlare-presente-noi-reverse",
  "pair_id": "it-verb-parlare-presente-noi",
  "direction": "english_to_italian",
  "front": {
    "text": "we speak / we are speaking",
    "language": "English",
    "image": null,
    "labels": ["verb", "presente", "noi"]
  },
  "back": {
    "text": "parliamo",
    "language": "Italian",
    "audio": "parliamo.mp3",
    "labels": ["verb", "presente", "noi"]
  }
}
```

---

## 15. Suggested deck structure

The system could generate several different decks or subdecks.

### Option A: By frequency band

* Italian verbs 1–500
* Italian verbs 501–1000
* Italian verbs 1001–2000

### Option B: By tense

* Italian verbs: presente
* Italian verbs: passato prossimo
* Italian verbs: imperfetto
* Italian verbs: futuro semplice
* Italian verbs: condizionale presente

### Option C: By learning task

* Recognise conjugated forms
* Produce conjugated forms
* Identify infinitives
* Translate example sentences

### Option D: Combined structure

Example:

* Italian::Verbs::Top 500::Presente
* Italian::Verbs::Top 500::Passato prossimo
* Italian::Verbs::Top 500::Imperfetto
* Italian::Verbs::Top 500::Infinitive recognition

---

## 15. Minimum viable version

The simplest useful version of the project could be:

1. Input: top 500 Italian words.
2. Detect verbs only.
3. Identify infinitives.
4. Generate conjugations for:

   * presente
   * passato prossimo
   * imperfetto
5. Generate recognition and production cards.
6. Export to Anki-compatible CSV.
7. Manually review flagged irregular or ambiguous verbs.

---

## 16. Noun-processing workflow

Nouns are the next major workflow after verbs.

Unlike verbs, where one infinitive generates conjugation tables, a noun generates a set of article, plural, and preposition/article combination cards.

The goal is to help the learner recognise and produce the noun in realistic Italian noun phrases, not just memorise the bare dictionary form.

---

### 16.1 Noun input

The input is a single Italian noun from the frequency dictionary.

Examples:

* cane
* casa
* libro
* ragazza
* uomo
* amico
* scuola
* città

---

### 16.2 Noun analysis

For each noun, the system needs to identify:

1. Singular form
2. Plural form
3. Gender
4. Whether it begins with a vowel
5. Whether it requires lo / gli forms
6. Whether it is invariant in the plural
7. Whether it has irregular plural behaviour
8. English meaning

Example:

| Field               | Example   |
| ------------------- | --------- |
| Source word         | cane      |
| Singular            | cane      |
| Plural              | cani      |
| Gender              | masculine |
| English meaning     | dog       |
| Definite singular   | il cane   |
| Definite plural     | i cani    |
| Indefinite singular | un cane   |

---

## 17. Noun flashcard generation

Each noun should generate multiple flashcards.

The core noun cards are:

1. Bare singular form
2. Bare plural form
3. Singular with indefinite article
4. Singular with definite article
5. Plural with definite article
6. English-to-Italian production card
7. Italian-to-English recognition card
8. Gender recognition card
9. Article selection card
10. Articulated preposition cards

---

### 17.1 Basic noun cards

For the noun:

> cane

The system could generate:

| Card type            | Front                       | Back      |
| -------------------- | --------------------------- | --------- |
| Singular recognition | cane                        | dog       |
| Plural production    | What is the plural of cane? | cani      |
| Indefinite article   | a dog                       | un cane   |
| Definite singular    | the dog                     | il cane   |
| Definite plural      | the dogs                    | i cani    |
| Gender               | What gender is cane?        | Masculine |

---

### 17.2 Articles to generate

The system should generate article-based noun phrases.

#### Indefinite articles

| Gender / noun type                                    | Italian article | Example      |
| ----------------------------------------------------- | --------------- | ------------ |
| Masculine standard                                    | un              | un cane      |
| Masculine before z, s + consonant, gn, ps, x, y, etc. | uno             | uno studente |
| Feminine before consonant                             | una             | una casa     |
| Feminine before vowel                                 | un’             | un’amica     |

#### Definite articles

| Gender / noun type                                    | Singular | Plural | Example                    |
| ----------------------------------------------------- | -------- | ------ | -------------------------- |
| Masculine standard                                    | il       | i      | il cane / i cani           |
| Masculine before z, s + consonant, gn, ps, x, y, etc. | lo       | gli    | lo studente / gli studenti |
| Masculine before vowel                                | l’       | gli    | l’amico / gli amici        |
| Feminine before consonant                             | la       | le     | la casa / le case          |
| Feminine before vowel                                 | l’       | le     | l’amica / le amiche        |

---

## 18. Articulated preposition generation

Italian nouns need special handling because articles and prepositions combine into a large, rule-driven grid.

For this system, every noun should be expanded into both singular and plural noun phrases, and then each of those noun phrases should be combined with the relevant preposition/article combinations.

This means the noun workflow is not simply:

> noun → singular card → plural card

It is closer to:

> noun → singular/plural forms → correct articles → preposition + article grid → phrase cards

The important point is that the system should not manually store every phrase. It should generate the grid from a smaller set of grammatical metadata.

---

### 18.1 Required noun metadata

Before generating article/preposition cards, the system needs to know the noun’s article class in both singular and plural.

For each noun, the system should store:

| Field                       | Purpose                      | Example for cane | Example for casa | Example for amico |
| --------------------------- | ---------------------------- | ---------------- | ---------------- | ----------------- |
| singular                    | Base singular form           | cane             | casa             | amico             |
| plural                      | Base plural form             | cani             | case             | amici             |
| gender                      | Determines article family    | masculine        | feminine         | masculine         |
| singular definite article   | Used for singular grid       | il               | la               | l’                |
| plural definite article     | Used for plural grid         | i                | le               | gli               |
| singular indefinite article | Used for basic article cards | un               | una              | un                |
| article class               | Determines contractions      | il/i             | la/le            | l’/gli            |

Once this metadata exists, the rest of the grid can be generated programmatically.

---

### 18.2 Definite article grid

The system should first generate the correct singular and plural definite noun phrases.

| Noun     | Singular definite | Plural definite |
| -------- | ----------------- | --------------- |
| cane     | il cane           | i cani          |
| casa     | la casa           | le case         |
| amico    | l’amico           | gli amici       |
| studente | lo studente       | gli studenti    |
| zaino    | lo zaino          | gli zaini       |
| scuola   | la scuola         | le scuole       |

These forms become the input to the preposition/article generator.

---

### 18.3 Core contracted preposition/article grid

The main articulated-preposition grid combines five prepositions with seven definite articles.

| Preposition | il  | lo    | l’    | la    | i   | gli   | le    |
| ----------- | --- | ----- | ----- | ----- | --- | ----- | ----- |
| a           | al  | allo  | all’  | alla  | ai  | agli  | alle  |
| di          | del | dello | dell’ | della | dei | degli | delle |
| da          | dal | dallo | dall’ | dalla | dai | dagli | dalle |
| in          | nel | nello | nell’ | nella | nei | negli | nelle |
| su          | sul | sullo | sull’ | sulla | sui | sugli | sulle |

For each noun, the system should select only the two relevant article columns:

1. The noun’s singular definite article
2. The noun’s plural definite article

Then it should generate every row for those two columns.

For example, cane uses:

* singular article: il
* plural article: i

So the system generates:

| Preposition | Singular phrase | Plural phrase |
| ----------- | --------------- | ------------- |
| a           | al cane         | ai cani       |
| di          | del cane        | dei cani      |
| da          | dal cane        | dai cani      |
| in          | nel cane        | nei cani      |
| su          | sul cane        | sui cani      |

For casa, the system uses:

* singular article: la
* plural article: le

So it generates:

| Preposition | Singular phrase | Plural phrase |
| ----------- | --------------- | ------------- |
| a           | alla casa       | alle case     |
| di          | della casa      | delle case    |
| da          | dalla casa      | dalle case    |
| in          | nella casa      | nelle case    |
| su          | sulla casa      | sulle case    |

For amico, the system uses:

* singular article: l’
* plural article: gli

So it generates:

| Preposition | Singular phrase | Plural phrase |
| ----------- | --------------- | ------------- |
| a           | all’amico       | agli amici    |
| di          | dell’amico      | degli amici   |
| da          | dall’amico      | dagli amici   |
| in          | nell’amico      | negli amici   |
| su          | sull’amico      | sugli amici   |

---

### 18.4 Non-contracted preposition/article phrases

Some common prepositions normally do not contract with the article in modern Italian.

The system should still generate cards for them, but they should be treated as separate phrase cards rather than articulated-preposition cards.

Core non-contracted prepositions:

| Preposition | Meaning              |
| ----------- | -------------------- |
| con         | with                 |
| per         | for / through        |
| tra         | between / among / in |
| fra         | between / among / in |

These combine as:

> preposition + definite article + noun

For cane:

| Preposition | Singular phrase | Plural phrase |
| ----------- | --------------- | ------------- |
| con         | con il cane     | con i cani    |
| per         | per il cane     | per i cani    |
| tra         | tra il cane     | tra i cani    |
| fra         | fra il cane     | fra i cani    |

For casa:

| Preposition | Singular phrase | Plural phrase |
| ----------- | --------------- | ------------- |
| con         | con la casa     | con le case   |
| per         | per la casa     | per le case   |
| tra         | tra la casa     | tra le case   |
| fra         | fra la casa     | fra le case   |

Optional older forms such as col, coi, pel, pei should be excluded from the MVP unless the deck is intended to support literary or advanced recognition.

---

### 18.5 Noun phrase generation matrix

For each noun, the system should generate a matrix of phrase outputs.

At minimum, each noun generates:

| Category                           | Singular |                Plural |
| ---------------------------------- | -------: | --------------------: |
| Bare noun                          |        1 |                     1 |
| Definite article phrase            |        1 |                     1 |
| Indefinite article phrase          |        1 | usually 0 or optional |
| Contracted preposition phrases     |        5 |                     5 |
| Non-contracted preposition phrases |        4 |                     4 |

This means a normal noun produces at least:

* 1 bare singular card item
* 1 bare plural card item
* 1 indefinite singular phrase
* 1 definite singular phrase
* 1 definite plural phrase
* 10 contracted preposition phrases
* 8 non-contracted preposition phrases

That is 22 Italian phrase items before bidirectional expansion.

Because cards are bidirectional, this becomes approximately:

> 22 phrase items × 2 directions = 44 flashcards per noun

The exact number may vary depending on whether the system includes indefinite plural forms, optional phrase cards, image cards, and advanced forms.

---

### 18.6 Generator logic

The articulated-preposition generator should work like this:

1. Receive noun metadata.
2. Identify singular noun form.
3. Identify plural noun form.
4. Identify singular definite article.
5. Identify plural definite article.
6. Look up the contracted form for each core preposition and article.
7. Combine the contracted form with the noun.
8. Generate singular phrase items.
9. Generate plural phrase items.
10. Generate card labels.
11. Send all phrase items to the bidirectional flashcard generator.

Pseudo-logic:

```text
for noun in nouns:
    singular_article = noun.definite_article.singular
    plural_article = noun.definite_article.plural

    singular_form = noun.singular
    plural_form = noun.plural

    for preposition in [a, di, da, in, su]:
        singular_contracted = contracted_grid[preposition][singular_article]
        plural_contracted = contracted_grid[preposition][plural_article]

        generate_phrase(singular_contracted + " " + singular_form)
        generate_phrase(plural_contracted + " " + plural_form)

    for preposition in [con, per, tra, fra]:
        generate_phrase(preposition + " " + singular_article + " " + singular_form)
        generate_phrase(preposition + " " + plural_article + " " + plural_form)
```

Apostrophe forms need spacing rules:

| Form type    | Correct output | Incorrect output |
| ------------ | -------------- | ---------------- |
| l’ + noun    | l’amico        | l’ amico         |
| all’ + noun  | all’amico      | all’ amico       |
| dell’ + noun | dell’amico     | dell’ amico      |
| dall’ + noun | dall’amico     | dall’ amico      |
| nell’ + noun | nell’amico     | nell’ amico      |
| sull’ + noun | sull’amico     | sull’ amico      |

---

### 18.7 Labels for generated noun/preposition cards

Each generated card should include labels that make the grammar visible without long explanations.

Examples:

| Phrase      | Labels                                                     |
| ----------- | ---------------------------------------------------------- |
| il cane     | noun, masculine, singular, definite article                |
| i cani      | noun, masculine, plural, definite article                  |
| al cane     | noun, masculine, singular, articulated preposition, a + il |
| ai cani     | noun, masculine, plural, articulated preposition, a + i    |
| della casa  | noun, feminine, singular, articulated preposition, di + la |
| nelle case  | noun, feminine, plural, articulated preposition, in + le   |
| con il cane | noun, masculine, singular, preposition phrase, con + il    |

---

### 18.8 Flashcard examples from the grid

For the noun:

> casa

Generated phrase item:

> alla casa

Italian-to-English card:

| Section | Content                                                   |
| ------- | --------------------------------------------------------- |
| Front   | alla casa                                                 |
| Labels  | noun, feminine, singular, articulated preposition, a + la |
| Back    | to the house                                              |

English-to-Italian card:

| Section | Content                                                   |
| ------- | --------------------------------------------------------- |
| Front   | to the house                                              |
| Labels  | noun, feminine, singular, articulated preposition, a + la |
| Back    | alla casa                                                 |

For the noun:

> amici

Generated phrase item:

> degli amici

Italian-to-English card:

| Section | Content                                                    |
| ------- | ---------------------------------------------------------- |
| Front   | degli amici                                                |
| Labels  | noun, masculine, plural, articulated preposition, di + gli |
| Back    | of the friends / some friends                              |

Note: di + article forms can sometimes overlap with partitive meanings. The system should allow notes or alternate meanings where needed.

---

### 18.9 Recommended MVP handling

For the MVP, every noun should generate:

1. Bare singular
2. Bare plural
3. Indefinite singular
4. Definite singular
5. Definite plural
6. a + definite article, singular and plural
7. di + definite article, singular and plural
8. da + definite article, singular and plural
9. in + definite article, singular and plural
10. su + definite article, singular and plural
11. con + definite article, singular and plural
12. per + definite article, singular and plural
13. tra + definite article, singular and plural
14. fra + definite article, singular and plural

Each phrase item then produces:

1. Italian-to-English card
2. English-to-Italian card

This makes the noun workflow highly generative, consistent, and easy to extend to other languages later.

---

## 19. Noun workflow diagram

```mermaid
flowchart TD
    A[Frequency dictionary word] --> B[Word analysis]
    B --> C{Is the word a noun?}

    C -->|No| D[Route to another word-type workflow]
    C -->|Yes| E[Noun analysis]

    E --> F[Identify singular form]
    E --> G[Identify plural form]
    E --> H[Identify gender]
    E --> I[Identify singular article]
    E --> J[Identify plural article]
    E --> K[Identify English meaning]

    F --> L[Noun metadata record]
    G --> L
    H --> L
    I --> L
    J --> L
    K --> L

    L --> M[Generate basic noun phrase items]
    M --> M1[Bare singular]
    M --> M2[Bare plural]
    M --> M3[Indefinite singular]
    M --> M4[Definite singular]
    M --> M5[Definite plural]

    L --> N[Generate contracted preposition grid]
    N --> N1[Use singular article column]
    N --> N2[Use plural article column]

    N1 --> O1[a + article + singular noun]
    N1 --> O2[di + article + singular noun]
    N1 --> O3[da + article + singular noun]
    N1 --> O4[in + article + singular noun]
    N1 --> O5[su + article + singular noun]

    N2 --> P1[a + article + plural noun]
    N2 --> P2[di + article + plural noun]
    N2 --> P3[da + article + plural noun]
    N2 --> P4[in + article + plural noun]
    N2 --> P5[su + article + plural noun]

    L --> Q[Generate non-contracted preposition phrases]
    Q --> Q1[con + article + noun]
    Q --> Q2[per + article + noun]
    Q --> Q3[tra + article + noun]
    Q --> Q4[fra + article + noun]
    Q1 --> R[Generate singular and plural forms]
    Q2 --> R
    Q3 --> R
    Q4 --> R

    M1 --> S[Phrase item list]
    M2 --> S
    M3 --> S
    M4 --> S
    M5 --> S
    O1 --> S
    O2 --> S
    O3 --> S
    O4 --> S
    O5 --> S
    P1 --> S
    P2 --> S
    P3 --> S
    P4 --> S
    P5 --> S
    R --> S

    S --> T[Add labels]
    T --> U[Generate Italian to English cards]
    T --> V[Generate English to Italian cards]
    U --> W[Validation and deduplication]
    V --> W
    W --> X[Export to Anki-compatible format]
```

---

## 20. Example noun output

For the noun:

> casa

The system may generate a structured output like this:

```json
{
  "source_word": "casa",
  "word_type": "noun",
  "language": "Italian",
  "english": "house",
  "gender": "feminine",
  "singular": "casa",
  "plural": "case",
  "articles": {
    "indefinite_singular": "una casa",
    "definite_singular": "la casa",
    "definite_plural": "le case"
  },
  "articulated_prepositions": {
    "singular": {
      "a": "alla casa",
      "di": "della casa",
      "da": "dalla casa",
      "in": "nella casa",
      "su": "sulla casa"
    },
    "plural": {
      "a": "alle case",
      "di": "delle case",
      "da": "dalle case",
      "in": "nelle case",
      "su": "sulle case"
    }
  },
  "non_contracted_prepositions": {
    "singular": {
      "con": "con la casa",
      "per": "per la casa",
      "tra": "tra la casa",
      "fra": "fra la casa"
    },
    "plural": {
      "con": "con le case",
      "per": "per le case",
      "tra": "tra le case",
      "fra": "fra le case"
    }
  }
}
```

---

## 21. Noun-specific quality control

Noun outputs need validation because Italian noun rules have many edge cases.

The system should check:

1. Gender is correct.
2. Plural is correct.
3. Definite article is correct.
4. Indefinite article is correct.
5. Apostrophes are handled correctly.
6. Masculine lo/gli nouns are detected correctly.
7. Invariant nouns are handled correctly.
8. Irregular plurals are flagged.
9. Ambiguous nouns are flagged.
10. Articulated prepositions are generated from the correct article.

Examples of edge cases:

| Noun     | Issue                                                                               |
| -------- | ----------------------------------------------------------------------------------- |
| uomo     | irregular plural: uomini                                                            |
| città    | invariant plural: città                                                             |
| amico    | plural spelling change: amici                                                       |
| studente | masculine but uses lo/gli pattern in some phrases only if required by sound pattern |
| problema | masculine despite ending in -a                                                      |
| mano     | feminine despite ending in -o                                                       |
| uovo     | masculine singular, feminine plural in some traditional grammar contexts: le uova   |

---

## 22. SQLite database design

The system should use an SQLite database as the main storage layer.

A key design requirement is that the original frequency-list word and the analysed language item should not be treated as the same thing.

One input word can produce one or more valid linguistic analyses.

For example, an Italian surface form may be:

* a noun
* a conjugated verb
* an adjective
* a preposition
* both a noun and a verb
* both a past participle and an adjective
* ambiguous until reviewed

Therefore, the database should separate:

1. The raw input word from the frequency dictionary.
2. The AI-generated classification results.
3. The generated word-specific language data.
4. The final flashcard outputs.

---

### 22.1 Core principle

The input word table should store each original word once.

The classification table should allow multiple analyses per input word.

This avoids a problem where a word that is both a noun and a verb gets forced into only one category.

Example:

> ballo

Possible analyses:

| Surface word | Analysis type | Explanation                      |
| ------------ | ------------- | -------------------------------- |
| ballo        | noun          | a dance / ball                   |
| ballo        | verb          | io ballo = I dance, from ballare |

The system should keep both analyses.

---

## 23. Table 1: input_words

This table stores every word from the frequency dictionary.

Each input word appears once.

### Purpose

* Preserve the original frequency dictionary.
* Store the surface form exactly as provided.
* Track frequency rank and source metadata.
* Provide the starting point for classification.

### Suggested schema

```sql
CREATE TABLE input_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface_word TEXT NOT NULL,
    normalised_word TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'Italian',
    frequency_rank INTEGER,
    frequency_value REAL,
    source_dictionary TEXT,
    processing_status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(language, normalised_word, source_dictionary)
);
```

### Example row

| id | surface_word | normalised_word | language | frequency_rank | processing_status |
| -: | ------------ | --------------- | -------- | -------------: | ----------------- |
|  1 | ballo        | ballo           | Italian  |            742 | classified        |

---

## 24. Table 2: word_analyses

This is one of the most important tables in the system.

It stores the AI classification results for each input word.

One input word can have many analyses.

### Purpose

* Store the word type identified by AI.
* Allow multiple classifications per word.
* Store confidence scores.
* Store language-specific fields such as infinitive or gender.
* Flag ambiguity for manual review.

### Defined word-type groups

The classification engine should use a controlled list of word types.

Recommended MVP word types:

| Word type    | Description                                             | Example                |
| ------------ | ------------------------------------------------------- | ---------------------- |
| verb         | Infinitive, conjugated verb, participle, or verbal form | parlare, parlo, fatto  |
| noun         | Person, place, thing, concept                           | casa, cane, ballo      |
| adjective    | Descriptive word                                        | bello, grande          |
| adverb       | Modifies verb/adjective/sentence                        | bene, sempre           |
| preposition  | Relationship word                                       | a, di, da, in, con, su |
| article      | Definite or indefinite article                          | il, lo, la, un         |
| pronoun      | Replaces noun or noun phrase                            | io, mi, lo, questo     |
| conjunction  | Connects words or clauses                               | e, ma, perché          |
| interjection | Exclamation or discourse marker                         | ciao, ah, boh          |
| numeral      | Number word                                             | uno, due, primo        |
| determiner   | Demonstratives, possessives, quantifiers, etc.          | questo, mio, ogni      |
| phrase       | Multi-word expression, if source allows it              | per favore             |
| unknown      | Could not clas                                          |                        |

This project creates a generalisable language-learning pipeline that starts with frequency-ranked words and produces structured flashcard decks.

For Italian verbs, the core workflow is:

1. Take a word from the frequency dictionary.
2. Determine whether it is a verb.
3. If it is a conjugated verb, identify the infinitive.
4. Generate conjugations across key tenses.
5. Generate forms for the relevant grammatical persons.
6. Validate the output.
7. Transform the output into flashcards.
8. Export the flashcards for spaced-repetition learning.

The first version should focus on verbs because they provide a clear, high-value workflow. Once the verb workflow is stable, the same system can be extended to nouns, adjectives, prepositions, pronouns, and other word classes.
