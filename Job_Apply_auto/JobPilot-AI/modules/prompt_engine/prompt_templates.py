# modules/prompt_engine/prompt_templates.py
from langchain_core.prompts import ChatPromptTemplate

'''
=====================================================================================================
Base Template
=====================================================================================================
'''
def base_prompt(context: str, question: str) -> str:
    template = """You are answering a job application question on behalf of the applicant.

Answer ONLY from the context below. The context is the applicant's own data.
If the context does not support an answer, reply with exactly: UNKNOWN
Never guess, never assume, and never state anything about the applicant that
the context does not contain — a wrong answer here is a false statement on a
real job application.
Respond as if you are the applicant. Your answer should be clear, direct, and concise.
Do not mention the context, your reasoning process, or how the answer was formed.

<context>
{context}
</context>

<question>
{question}
</question>

Return a direct short answer without repeating the question (maximum 10 words),
or exactly UNKNOWN if the context does not support one.
"""

    return ChatPromptTemplate.from_template(template).format_messages(
        context=context or "N/A", 
        question=question
    )[0].content


'''
=====================================================================================================
Options Prompt Template
=====================================================================================================
'''
def options_prompt(context: str, question: str, options: list[str], multi_select: bool = False) -> str:

    choices = "\n".join([f"- {opt}" for opt in options])
    
    instruction = (
        "Select *all* options the context supports."
        if multi_select else
        "Select the *one option* the context supports."
    )

    template = """You are answering a job application question on behalf of the applicant.

Use the context below — the applicant's own data — to select the appropriate {return_format}.
If no option is supported by the context, reply with exactly: UNKNOWN
Never guess and never select an option that states something about the applicant
the context does not contain.
Respond as if you are the applicant.

<context>
{context}
</context>

<question>
{question}
</question>

<options>
{choices}
</options>

{instruction}

Return only the exact text of the selected {return_format}, with no explanations or additional comments. Do not repeat the question, and do not mention the context or your reasoning.
If none is supported by the context, return exactly UNKNOWN rather than picking the closest one.
Do not mention the context, reasoning process, or how you chose the answer.
"""

    prompt = ChatPromptTemplate.from_template(template)
    
    return prompt.format_messages(
        context=context,
        question=question,
        choices=choices,
        instruction=instruction,
        return_format="options (as a list)" if multi_select else "option",
        choice_scope="one(s)" if multi_select else "one"
    )[0].content

