#!/usr/bin/env python3
#
import concurrent
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from os import getcwd
from time import time
from typing import Iterator, List, Optional, Tuple

from git import Repo, Commit
from github import Repository, PullRequest, PullRequestPart, Branch
from github.AuthenticatedUser import AuthenticatedUser
from github.MainClass import Github
from plumbum import cli, local


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
    __no_rebase_on_root = False
    __github: Github = None

    @cli.autoswitch(str, mandatory=True)
    def github_token(self, token: str):
        """
        Github token (needs to be able to read repositories)
        """
        self.__github = Github(token)

    @cli.autoswitch(bool)
    def no_rebase_on_root(self, value: bool):
        """
        Do not rebase onto root even if root has changed
        """
        self.__no_rebase_on_root = value

    def main(self):
        git = local["git"]

        origin: str = git("config", "--get", "remote.origin.url")
        origin = origin.strip()
        repo = self.__get_repo(origin)
        if not repo:
            raise Exception("Unable to find repo for %s in your GitHub account" % origin)

        prs = list(self.__get_prs(repo))
        roots = self.__get_pr_tree(prs)
        self.__print(roots)

    def __print(self, roots: List[TreeNode]):
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

    def __get_pr_tree(self, prs: List[PrInfo]) -> List[TreeNode]:
        head_to_base = {}
        head_to_pr = {}
        for pr in prs:
            head_to_base[pr.head_branch_name] = pr.base_branch_name
            head_to_pr[pr.head_branch_name] = pr

        def get_pr(branch: str) -> Optional[PrInfo]:
            if branch in head_to_pr:
                return head_to_pr[branch]
            else:
                return None

        # BFS
        leafs = {
            b: TreeNode(base_node=None,
                        head_branch=b,
                        pr_info=None)
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

    def __get_prs(self, repo: Repository) -> Iterator[PrInfo]:
        user: AuthenticatedUser = self.__github.get_user()
        login = user.login
        issues = self.__github.search_issues("", type="pr", state="open", author=login, repo=repo.full_name)
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

    def __remote_sha(self, repo: Repository, branch: str) -> str:
        branch: Branch = repo.get_branch(branch)
        commit: Commit = branch.commit
        return commit.sha

    def __get_repo(self, git_url: str) -> Optional:
        self.__github.get_repos()
        user: AuthenticatedUser = self.__github.get_user()
        for repo in user.get_repos():
            repo: Repository
            if repo.ssh_url == git_url:
                return repo
        return None


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
