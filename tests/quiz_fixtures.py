import json


def lesson_text(identity="base-lesson", pages=("base-page",), locale="en", body="[wort1]{.tl}", roles=""):
    metadata = f"---\ngmd: 0.3\nid: {identity}\ntitle: Test lesson\nlang: de\nexplained_in: {locale}\nlevel: A1\n{roles}---\n"
    return metadata + "".join(f"# Page {index + 1} {{#{page}}}\n\n{body}\n\n" for index, page in enumerate(pages))


def quiz_text(identity="base-bank", lesson="base-lesson", page="base-page", prefix="base", count=25, locale="en", options=None, explanation="The form is [wort1]{.tl}."):
    text = f"---\nqmd: 0.2\nid: {identity}\nlesson: {lesson}\ntitle: Test quiz\nlang: de\nexplained_in: {locale}\n---\n"
    for index in range(count):
        metadata = {"type": "blank" if options is None else "choice", "page": page, "skill": "form"}
        if options is None:
            metadata["answers"] = ["wort1"]
        else:
            metadata["options"] = [{"id": f"option-{number}", "text": f"wort{number + 1}", "correct": number == 0} for number in range(options)]
        text += f"\n# {prefix}-{index}\n```quiz\n{json.dumps(metadata)}\n```\n## Prompt\nChoose the form.\n## Sentence\n{{{{blank}}}}\n## Explanation\n{explanation}\n"
    return text
