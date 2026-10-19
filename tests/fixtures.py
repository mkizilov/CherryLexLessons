import json


def pair(locale="en", checkpoint=20):
    identity = f"de-checkpoint-{checkpoint}"
    lesson = f"---\ngmd: 0.3\nid: {identity}\ntitle: Test lesson\nlang: de\nexplained_in: {locale}\nlevel: A1\n---\n# Example {{#example}}\n\n[wort1]{{.tl}}\n"
    quiz = f"---\nqmd: 0.2\nid: {identity}-quiz\nlesson: {identity}\ntitle: Test quiz\nlang: de\nexplained_in: {locale}\n---\n"
    for index in range(25):
        quiz += f'\n# q-{index}\n```quiz\n{{"type":"blank","page":"example","skill":"form","answers":["wort1"]}}\n```\n## Prompt\nChoose the form.\n## Sentence\n{{{{blank}}}}\n## Explanation\nThe form is wort1.\n'
    return lesson, quiz


def choice_pair(locale="en", *, text="wort1", sentence="wort1", prompt="Choose the matching form."):
    lesson, quiz = pair(locale)
    options = [{"id": f"option-{index}", "text": text if index == 0 else f"wort{index + 1}", "correct": index == 0} for index in range(4)]
    metadata = {"type": "choice", "page": "example", "skill": "form", "options": options}
    question = f'\n# q-choice\n```quiz\n{json.dumps(metadata)}\n```\n## Prompt\n{prompt}\n## Sentence\n{sentence}\n## Explanation\nThe form matches.\n'
    return lesson, quiz[:quiz.index("\n# q-0")] + question + quiz[quiz.index("\n# q-1"):]


def return_pair(locale="en", base=20, stage=1, mode="practice"):
    lesson, quiz = pair(locale, base)
    identity = f"de-checkpoint-{base}"
    quiz = quiz.replace(f"id: {identity}-quiz", f"id: {identity}-return-{stage}-quiz")
    quiz = quiz.replace("# q-", f"# r{stage}-q-")
    quiz = quiz.replace("Choose the form.", f"Return {stage}: recall the form.")
    if mode == "practice":
        lesson = ""
    else:
        lesson = lesson.replace(f"id: {identity}", f"id: {identity}-return-{stage}")
        lesson = lesson.replace("{#example}", f"{{#extension-{stage}}}")
        quiz = quiz.replace('"page":"example"', f'"page":"extension-{stage}"')
    return lesson, quiz
