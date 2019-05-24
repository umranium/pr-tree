#!/usr/bin/env python3
#
import os
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple, Iterable, Dict

from github import Repository, PullRequest, Issue, PullRequestReview
from github.AuthenticatedUser import AuthenticatedUser
from github.MainClass import Github
from plumbum import cli, local, FG
from plumbum.cli import switch

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

    def head_sha(self) -> str:
        return self._pr.head.sha

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


@PrTree.subcommand("update-dependencies")
class UpdateDependencies(cli.Application):
    """
    Updates all the dependent PRs of a PR by recursively rebasing them
    """
    __user: AuthenticatedUser
    __repo: RemoteRepo
    __root: str
    __dry_run = cli.Flag("--dry-run",
                         help="Show sequence of steps but do not make any changes")
    __delete = cli.Flag("--delete",
                        help="Whether or not to delete the root branch while rebasing. "
                             "Deleting results in the children rebasing onto the root's parent and not itself. "
                             "Deleting has no effect if the branch is the root branch (i.e. master). ")

    @switch("--root", str, mandatory=True)
    def root(self, value: str):
        """
        The root branch. All branches based on this branch will be rebased recursively.
        """
        self.__root = value

    # noinspection PyStatementEffect
    def main(self):
        self.__user = github.get_user()

        self.__repo = get_repo(self.__user)

        print("fetching PRs")
        prs = list(self.__repo.get_user_prs(self.__user))
        roots = create_tree(prs)

        @dataclass()
        class RebaseStep:
            base: TreeNode
            base_initial_local_sha: str
            child: TreeNode

        rebase_steps: List[RebaseStep] = []
        for node, ancestry in _depth_first(roots):
            if not node.base_node:  # can't rebase root
                continue
            base = node.base_node
            ancestor_heads = set(a.head_branch for a in ancestry)
            if self.__root not in ancestor_heads:
                continue
            if self.__delete and base.head_branch == self.__root and base.base_node:
                base = base.base_node
            step = RebaseStep(base=base,
                              base_initial_local_sha=get_local_sha(base.head_branch),
                              child=node)
            rebase_steps.append(step)

        git = local["git"]
        for step in rebase_steps:
            print("Rebasing", step.child.head_branch,
                  "onto", step.base.head_branch,
                  "starting from", step.base_initial_local_sha)

            if self.__dry_run:
                continue

            while True:
                try:
                    git["rebase", "-i",
                        "--onto", step.base.head_branch,
                        step.base_initial_local_sha, step.child.head_branch] & FG
                    break
                except Exception as e:
                    print(e)

            git["push", "-f"] & FG


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
        for node, parentage in _depth_first(roots):
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
                local_differs = node.pr_info.head_sha() != get_local_sha(node.pr_info.head_branch_name())
                if local_differs:
                    line_segments.append(" 🌓")
                else:
                    line_segments.append(" 🌕")
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


def _depth_first(nodes: List[TreeNode]) -> Iterator[Tuple[TreeNode, List[TreeNode]]]:
    def transverse(node: TreeNode, chain: List[TreeNode]) -> Iterator[Tuple[TreeNode, List[TreeNode]]]:
        yield (node, chain)
        for m in node.children:
            yield from transverse(m, chain + [node])

    for n in nodes:
        yield from transverse(n, [])


def _breadth_first(nodes: List[TreeNode]) -> Iterator[Tuple[TreeNode, List[TreeNode]]]:
    queue = [(n, []) for n in nodes]
    while queue:
        node, chain = queue.pop(0)
        yield node, chain
        for n in node.children:
            queue.append((n, chain + [node]))


def get_local_sha(branch_name: str) -> str:
    git = local["git"]
    result: str = git("rev-parse", branch_name)
    return result.strip()


if __name__ == '__main__':
    PrTree.run()
