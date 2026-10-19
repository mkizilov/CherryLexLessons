---
qmd: 0.2
id: example-bank
lesson: example-lesson
title: Example quiz
lang: de
explained_in: en
---

# whole-sentence-choice
```quiz
{"type":"choice","page":"word-order","skill":"intended-statement","options":[{"id":"verb-last","text":"Ich hier bin.","correct":false},{"id":"other-person","text":"Du bist hier.","correct":false},{"id":"natural","text":"Ich bin hier.","correct":true},{"id":"question-order","text":"Bin ich hier?","correct":false},{"id":"wrong-agreement","text":"Ich ist hier.","correct":false},{"id":"negative","text":"Ich bin nicht hier.","correct":false}]}
```
## Prompt
Choose the neutral statement meaning “I am here.”
## Explanation
[Ich bin hier.]{.tl} reports the speaker's presence. The other options change the person, polarity or statement/question meaning, or use an incorrect form/order.

# typed-gap
```quiz
{"type":"blank","page":"word-order","skill":"verb-agreement","answers":["bin"],"case":"exact"}
```
## Prompt
Complete the statement meaning “I am here.”
## Sentence
Ich {{blank}} hier.
## Explanation
Use [bin]{.tl} with [Ich]{.tl}: [Ich bin hier.]{.tl}
