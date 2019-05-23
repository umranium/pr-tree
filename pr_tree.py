#!/usr/bin/env python3
#
import os
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from os import getcwd
from typing import Iterator, List, Optional, Tuple, Iterable, Callable

from git import Repo, Commit
from github import Repository, PullRequest, PullRequestPart, Branch, Issue
from github.AuthenticatedUser import AuthenticatedUser
from github.MainClass import Github
from plumbum import cli, local
from plumbum.cli import switch

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
if not GITHUB_TOKEN:
    raise Exception("GitHub token not specified in environment. Please set GITHUB_TOKEN")


@dataclass
class PrInfo:
    pr_number: int
    head_branch_name: str
    base_branch_name: str


@dataclass
class TreeNode:
    base_node: Optional['TreeNode']
    head_branch: str
    pr_info: Optional[PrInfo]
    children: List['TreeNode'] = field(default_factory=list)

    def is_root(self) -> bool:
        if not self.base_node:
            return True
        else:
            return False

    def has_children(self) -> bool:
        if self.children:
            return True
        else:
            return False

    def is_last_sibling(self) -> bool:
        if not self.base_node:
            return True
        index = self.base_node.children.index(self)
        sibling_count = len(self.base_node.children)
        return index == (sibling_count - 1)


class GitChain(cli.Application):
    __github: Github = Github(GITHUB_TOKEN)
    __task: Callable[[], None]

    @cli.switch("print", mandatory=True)
    def print(self):
        self.__task = self.__print_task

    def main(self):
        self.__task()

    def __print_task(self):
        git = local["git"]

        origin: str = git("config", "--get", "remote.origin.url")
        origin = origin.strip()

        user: AuthenticatedUser = self.__github.get_user()

        repo = self.__get_repo(user, origin)
        if not repo:
            raise Exception("Unable to find repo for %s in your GitHub account" % origin)

        prs = list(self.__get_prs(user, repo))
        roots = create_tree(prs)
        print_trees(roots)

    def __get_prs(self, user: AuthenticatedUser, repo: Repository) -> Iterator[PrInfo]:
        issues: Iterable[Issue] = self.__github.search_issues(
            "", type="pr", state="open", author=user.login, repo=repo.full_name)
        pr_numbers: List[int] = [issue.number for issue in issues]

        with ThreadPoolExecutor(max_workers=3) as executor:
            prs: List[PullRequest] = executor.map(repo.get_pull, pr_numbers)

        for pr in prs:
            head: PullRequestPart = pr.head
            base: PullRequestPart = pr.base
            yield PrInfo(
                pr_number=pr.number,
                head_branch_name=head.ref,
                base_branch_name=base.ref
            )

    def __get_repo(self, user: AuthenticatedUser, git_url: str) -> Optional:
        for repo in user.get_repos():
            repo: Repository
            if repo.ssh_url == git_url or repo.html_url == git_url:
                return repo
        return None

    def __remote_sha(self, repo: Repository, branch: str) -> str:
        branch: Branch = repo.get_branch(branch)
        commit: Commit = branch.commit
        return commit.sha


def print_trees(roots: List[TreeNode]):
    for node, _ in _depth_first(roots):
        parentage = _ancestry(node)
        line_segments = []
        for p in parentage:
            if p.is_last_sibling():
                line_segments.append(" ")
            else:
                line_segments.append("│")
        if node.is_root():
            line_segments.append("─")
        elif node.is_last_sibling():
            line_segments.append("└")
        else:
            line_segments.append("├")

        if node.has_children():
            line_segments.append("┬ ")
        else:
            line_segments.append("─ ")

        line_segments.append(node.head_branch)
        if node.pr_info:
            line_segments.append(" [%d]" % node.pr_info.pr_number)
        print("".join(line_segments))


def create_tree(prs: List[PrInfo]) -> List[TreeNode]:
    head_to_base = {}
    head_to_pr = {}
    for pr in prs:
        head_to_base[pr.head_branch_name] = pr.base_branch_name
        head_to_pr[pr.head_branch_name] = pr

    def get_pr(branch: str) -> Optional[PrInfo]:
        return head_to_pr[branch] if branch in head_to_pr else None

    leafs = {
        b: TreeNode(base_node=None,
                    head_branch=b,
                    pr_info=get_pr(b))
        for _, b in head_to_base.items()
        if b not in head_to_base
    }
    roots = [v for k, v in leafs.items()]

    while leafs:
        leaf_branches = {l for l in leafs.keys()}
        next_leafs = {}
        for l in leaf_branches:
            for h, b in head_to_base.items():
                if b == l:
                    new_node = TreeNode(base_node=leafs[l],
                                        head_branch=h,
                                        pr_info=get_pr(h))
                    next_leafs[h] = new_node
                    leafs[l].children.append(new_node)

        leafs = next_leafs

    return roots


def _depth_first(nodes: List[TreeNode]) -> Iterator[Tuple[TreeNode, int]]:
    def transverse(node: TreeNode, depth: int) -> Iterator[Tuple[TreeNode, int]]:
        yield (node, depth)
        for m in node.children:
            yield from transverse(m, depth + 1)

    for n in nodes:
        yield from transverse(n, 0)


def _breadth_first(nodes: List[TreeNode]) -> Iterator[Tuple[TreeNode, int]]:
    queue = [(n, 0) for n in nodes]
    while queue:
        node, depth = queue.pop(0)
        yield node, depth
        for n in node.children:
            queue.append((n, depth + 1))


# Returns ancestors of node, from the oldest to it's parent
def _ancestry(node: TreeNode) -> List[TreeNode]:
    node = node.base_node
    nodes = []
    while node:
        nodes.append(node)
        node = node.base_node
    return list(reversed(nodes))


def local_sha(branch: str) -> str:
    commit: Commit = Repo(getcwd()).rev_parse(branch)
    return commit.hexsha


if __name__ == '__main__':
    GitChain.run()
