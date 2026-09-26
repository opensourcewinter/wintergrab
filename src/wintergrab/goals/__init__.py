"""Goals: say what data you want; wintergrab plans the crawl, shows it, and collects it.

::

    from wintergrab.goals import parse_goal, plan_goal

    goal = parse_goal("Find all laptops under $1000 on shop.example with name, price and rating")
    plan = plan_goal(goal)          # surveys the site: robots.txt, sitemaps, a sample of pages
    print(plan.describe())          # the steps, and what they will cost
    result = plan.run("laptops.jsonl")
    print(result.summary())

On the command line: ``wintergrab goal "..."``. See ``docs/goals.md``.
"""

from __future__ import annotations

from .goal import ENTITIES, EntityKind, Goal, GoalFilter, parse_goal
from .plan import Estimate, GoalPlan, SitePlan, path_pattern, plan_goal
from .reading import model_reader
from .run import GoalResult, GoalSpider, run_plan

__all__ = [
    "ENTITIES",
    "EntityKind",
    "Estimate",
    "Goal",
    "GoalFilter",
    "GoalPlan",
    "GoalResult",
    "GoalSpider",
    "SitePlan",
    "model_reader",
    "parse_goal",
    "path_pattern",
    "plan_goal",
    "run_plan",
]
