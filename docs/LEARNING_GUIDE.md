# Understanding and review guide

The assignment evaluates whether you understand the agent, not merely whether the output looks useful. Complete these exercises yourself and retain actual evidence.

## Explain the system in your own words
1. What does the LLM decide, and what does Python calculate deterministically?
2. Why is a missing sponsorship statement uncertainty rather than approval?
3. Why can operations leadership support a business analyst application without satisfying five years of professional data science?
4. What changes when a role requires current undergraduate enrollment?
5. Why can an exact quote still support an incorrect model interpretation?
6. What is the difference between a scripted replay and a real model/tool run?
7. What information persists after the chat closes? What is not yet implemented?

## First experiment
Run the fictional replay and tests. Then ask the coding agent to evaluate the five fictional postings without reading the expected assessments, using an explicitly synthetic profile with valid internship authorization. Save its decisions and tool transcript. Compare against expected outcomes. Report false recommendations, missed blockers and unnecessary verification separately. Five fixtures are a smoke test, not a statistical accuracy estimate.

Next, supply one real employer posting. Verify the source and compare every quote manually. Supply your actual graduation date/availability only when confirmed. A VERIFY result is acceptable when facts remain unknown.

## Add the thing only you know
Pick one real experience from your operations or Cherry Coatings work. Describe it accurately, add it to private/profile.json, rerun the same role and explain whether the decision should change. Do not add invented years, metrics or credentials.

## Your decision log
Keep these fields for each actual review:
- Date and role.
- What AI proposed.
- Evidence I checked.
- What I challenged or found missing.
- Change I made and why.
- Test or rerun result.
- What I still cannot explain.

Leave entries blank until you do the work. The initial proposed corrections (up to two targets, uncertainty handling, historical separation) were identified by AI from your requirements; do not present them as your independent discoveries unless that reflects your actual work.

## Demonstration outline
Explain the personal problem, show one posting, observe the agent selecting and calling tools, inspect its saved assessment, show a blocker case, and explain one correction you personally made. Show git history for real work. Distinguish prototype limitations from future features. Before committing to this hosted architecture for the final submission, ask the professor whether the mini-project requires a standalone Python model loop; the syllabus alone does not specify that interface.
