#!/usr/bin/env python3
#
import os
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple, Iterable, Dict

from github import Repository, PullRequest, Issue, PullRequestReview
from github.AuthenticatedUser import AuthenticatedUser
from github.MainClass import Github
from plumbum import cli, local

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
if not GITHUB_TOKEN:
    raise Exception("GitHub token not specified in environment. Please set GITHUB_TOKEN")
github: Github = Github(GITHUB_TOKEN)


class RemoteRepo:
    def __init__(self, repo: Repository):
        self._repo = repo

    def get_user_prs(self, user: AuthenticatedUser) -> List['PrInfo']:
        issues: Iterable[Issue] = github.search_issues(
            "", type="pr", state="open", author=user.login, repo=self._repo.full_name)
        pr_numbers: List[int] = [issue.number for issue in issues]

        with ThreadPoolExecutor() as executor:
            prs: List[PullRequest] = executor.map(self._repo.get_pull, pr_numbers)

        return [PrInfo(self, pr) for pr in prs]


@dataclass
class ReviewerState:
    reviewer: str
    state: str

    def to_emoji(self) -> str:
        if self.state == "APPROVED":
            return "✅"
        elif self.state == "CHANGES_REQUESTED":
            return "❌"
        elif self.state == "COMMENTED":
            return "💬"
        elif self.state == "PENDING":
            return "⏳"
        else:
            return self.state


class PrInfo:
    def __init__(self, repo: Repository, pr: PullRequest):
        self._repo = repo
        self._pr = pr

    def pr_number(self) -> int:
        return self._pr.number

    def head_branch_name(self) -> str:
        return self._pr.head.ref

    def base_branch_name(self) -> str:
        return self._pr.base.ref

    def pr_data(self) -> Dict:
        """
        library doesn't support some of the attributes, need to get the data ourselves
        :return:
        """
        if hasattr(self, '_pr_data'):
            return self._pr_data
        # noinspection PyProtectedMember
        _, pr_data = self._pr._requester.requestJsonAndCheck("GET", self._pr.url)
        setattr(self, '_pr_data', pr_data)
        return pr_data

    def reviewer_states(self, user: AuthenticatedUser) -> List[ReviewerState]:
        reviews: List[PullRequestReview] = list(self._pr.get_reviews())
        reviewer_states = {}

        for review in reviews:
            if review.user.login == user.login:  # skip reviews from author
                continue
            reviewer_states[review.user.login] = ReviewerState(reviewer=review.user.login, state=review.state)

        for requested_reviewer in self.pr_data()["requested_reviewers"]:
            login = requested_reviewer["login"]
            reviewer_states[login] = ReviewerState(reviewer=login, state="PENDING")

        return list(reviewer_states.values())


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


class PrTree(cli.Application):
    def main(self):
        if not self.nested_command:
            self.help()


@PrTree.subcommand("print")
class Print(cli.Application):
    """
    Prints the user's PRs in the form of a tree, where each node is placed below it's base branch
    """
    __user: AuthenticatedUser
    __repo: RemoteRepo

    def main(self):
        self.__user = github.get_user()

        self.__repo = get_repo(self.__user)

        print("fetching PRs")
        prs = list(self.__repo.get_user_prs(self.__user))
        roots = create_tree(prs)

        print("fetching reviews")

        def get_pr_reviewers(pr: PrInfo):
            return pr.pr_number(), pr.reviewer_states(self.__user)

        with ThreadPoolExecutor() as executor:
            reviewer_states = {k: v for k, v in executor.map(get_pr_reviewers, prs)}
        self.__print(roots, reviewer_states)

    def __print(self, roots: List[TreeNode], reviewer_states: Dict[int, List[ReviewerState]]):
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
                line_segments.append(" [%d]" % node.pr_info.pr_number())
                line_segments.append(" ")
                line_segments.append(",".join("%s:%s" % (rev_state.reviewer, rev_state.to_emoji())
                                              for rev_state in reviewer_states[node.pr_info.pr_number()]))
            print("".join(line_segments))


def get_repo(user: AuthenticatedUser) -> RemoteRepo:
    git = local["git"]
    origin: str = git("config", "--get", "remote.origin.url")
    origin = origin.strip()

    for repo in user.get_repos():
        repo: Repository
        if repo.ssh_url == origin or repo.html_url == origin:
            return RemoteRepo(repo)

    raise Exception("Unable to find repo for %s in your GitHub account" % origin)


def create_tree(prs: List[PrInfo]) -> List[TreeNode]:
    head_to_base = {}
    head_to_pr = {}
    for pr in prs:
        head_to_base[pr.head_branch_name()] = pr.base_branch_name()
        head_to_pr[pr.head_branch_name()] = pr

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


if __name__ == '__main__':
    PrTree.run()
