"""Declare skill repositories from an installed package (the declared configuration)."""

from daimon.core.server_skills.repo import valid_branch
from daimon.core.skill_repos import SkillRepoDecl, register_skill_repo_loader

__all__ = ["SkillRepoDecl", "register_skill_repo_loader", "valid_branch"]
