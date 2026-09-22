from __future__ import annotations

import ast
import inspect
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import DEFAULT, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import workflow_manager as manager


class RecordingPathRepositoryAdapter(manager.PathRepositoryAdapter):
    def __init__(self, root: Path):
        super().__init__(root)
        self.arguments: list[str] = []

    def normalize(self, relative: str) -> str:
        self.arguments.append(relative)
        return super().normalize(relative)


class RepositoryAdapterTests(unittest.TestCase):
    def test_windows_main_reaches_lifecycles_only_through_selected_adapter(self) -> None:
        main_tree = ast.parse(inspect.getsource(manager.main))
        lifecycle_calls = {
            node.func.id: node
            for node in ast.walk(main_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"install", "recover", "uninstall"}
        }
        self.assertEqual({"install", "recover", "uninstall"}, set(lifecycle_calls))
        for name, call in lifecycle_calls.items():
            with self.subTest(name=name):
                self.assertIsInstance(call.args[-1], ast.Name)
                self.assertEqual("adapter", call.args[-1].id)

        calls: list[tuple[str, object]] = []

        class SelectedAdapter:
            root = Path("C:/selected-native-root")

            def close(self) -> None:
                calls.append(("close", self))

        adapters = [SelectedAdapter() for _ in range(4)]

        def record(name: str):
            def operation(*args: object, **_kwargs: object) -> None:
                calls.append((name, args[-1]))

            return operation

        argument_sets = (
            ["workflow_manager.py", "install", "--target", "C:/target"],
            ["workflow_manager.py", "install", "--target", "C:/target"],
            [
                "workflow_manager.py",
                "install",
                "--target",
                "C:/target",
                "--recover",
            ],
            ["workflow_manager.py", "uninstall", "--target", "C:/target"],
        )
        forbidden = {
            "_assert_safe_target": manager._assert_safe_target,
            "_safe_read_bytes": manager._safe_read_bytes,
            "_safe_unlink": manager._safe_unlink,
            "_safe_rmdir": manager._safe_rmdir,
            "_atomic_write_managed": manager._atomic_write_managed,
        }
        with patch.object(
            manager,
            "_repository_adapter_from_raw_target",
            side_effect=adapters,
        ):
            with patch.object(
                manager.PathRepositoryAdapter,
                "__init__",
                side_effect=AssertionError("pathname adapter reached"),
            ):
                with patch.object(manager, "install", side_effect=record("install")):
                    with patch.object(manager, "recover", side_effect=record("recover")):
                        with patch.object(manager, "uninstall", side_effect=record("uninstall")):
                            with patch.multiple(
                                manager,
                                **{
                                    name: DEFAULT
                                    for name in forbidden
                                },
                            ) as tripwires:
                                for mock in tripwires.values():
                                    mock.side_effect = AssertionError(
                                        "pathname target I/O reached"
                                    )
                                for arguments in argument_sets:
                                    with patch.object(sys, "argv", arguments):
                                        self.assertEqual(0, manager.main())

        self.assertEqual(
            [
                ("install", adapters[0]),
                ("close", adapters[0]),
                ("install", adapters[1]),
                ("close", adapters[1]),
                ("recover", adapters[2]),
                ("close", adapters[2]),
                ("uninstall", adapters[3]),
                ("close", adapters[3]),
            ],
            calls,
        )

    def test_normalization_rejects_absolute_parent_and_noncanonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = manager.PathRepositoryAdapter(Path(directory))
            self.assertEqual("managed/file.txt", adapter.normalize("managed/file.txt"))
            for invalid in (
                "",
                ".",
                "./managed",
                "managed/../file.txt",
                "managed/./file.txt",
                "managed//file.txt",
                "managed/",
                "/managed/file.txt",
                "//server/share/file.txt",
                "//?/C:/managed/file.txt",
                "C:managed/file.txt",
                "C:/managed/file.txt",
                "managed\\file.txt",
                "\\\\server\\share\\file.txt",
                "\\\\?\\C:\\managed\\file.txt",
                "managed/file.txt\x00ignored",
                "managed/file.txt.",
                "managed/file.txt ",
                "CON",
                "nul.txt",
                "managed/PrN.config",
                "managed/COM1.log",
                "managed/lpt9.anything",
                "managed/CONIN$.txt",
                str(Path(directory) / "file.txt"),
            ):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(manager.WorkflowError) as raised:
                        adapter.normalize(invalid)
                    self.assertIs(type(raised.exception), manager.WorkflowError)

            with self.assertRaises(manager.WorkflowError) as raised:
                adapter.normalize(None)  # type: ignore[arg-type]
            self.assertIs(type(raised.exception), manager.WorkflowError)

    def test_path_adapter_contract_covers_repository_file_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = manager.PathRepositoryAdapter(root)
            adapter.mkdir("managed")
            adapter.atomic_write("managed/file.txt", b"content")
            self.assertTrue(adapter.exists("managed/file.txt", require_file=True))
            self.assertEqual(b"content", adapter.read_bytes("managed/file.txt"))
            self.assertEqual(
                manager._sha256_bytes(b"content"), adapter.hash_file("managed/file.txt")
            )
            adapter.unlink("managed/file.txt")
            self.assertFalse(adapter.exists("managed/file.txt"))
            adapter.rmdir("managed")
            self.assertFalse((root / "managed").exists())

    def test_transaction_core_uses_relative_action_and_backup_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = manager.PathRepositoryAdapter(root)
            relative = next(iter(manager.PACKAGE_FILES.values()))
            adapter.atomic_write(relative, b"before")
            actions = manager._prepare_relative_actions(
                adapter,
                "a" * 32,
                [("write", relative, b"after")],
            )

            self.assertEqual(relative, actions[0].path)
            self.assertEqual(
                f"{manager.BACKUP_DIRECTORY}/{'a' * 32}/{relative}",
                actions[0].backup,
            )
            self.assertIsInstance(actions[0].path, str)
            self.assertIsInstance(actions[0].backup, str)
            self.assertEqual(relative, actions[0].to_json()["path"])
            planned = manager._plan_rollback_directories(adapter, actions)
            self.assertEqual(
                [
                    manager.BACKUP_DIRECTORY,
                    f"{manager.BACKUP_DIRECTORY}/{'a' * 32}",
                    f"{manager.BACKUP_DIRECTORY}/{'a' * 32}/{manager.INSTALL_DIRECTORY}",
                ],
                planned,
            )

        adapter_types = [manager.PathRepositoryAdapter]
        if sys.platform == "win32":
            adapter_types.append(manager.WindowsRepositoryAdapter)
        for adapter_type in adapter_types:
            with self.subTest(adapter_type=adapter_type.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    adapter = adapter_type(Path(directory))
                    try:
                        relative = manager.PACKAGE_FILES["rules/ROUTING.md"]
                        actions = manager._prepare_relative_actions(
                            adapter,
                            "b" * 32,
                            [("write", relative, b"after")],
                        )
                        self.assertEqual(
                            [manager.INSTALL_DIRECTORY, f"{manager.INSTALL_DIRECTORY}/rules"],
                            manager._plan_rollback_directories(adapter, actions),
                        )
                        adapter.mkdir(manager.INSTALL_DIRECTORY)
                        self.assertEqual(
                            [f"{manager.INSTALL_DIRECTORY}/rules"],
                            manager._plan_rollback_directories(adapter, actions),
                        )
                    finally:
                        adapter.close()

    def test_install_uses_one_path_adapter_and_only_relative_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            adapter = RecordingPathRepositoryAdapter(target)
            with patch.object(manager, "_repository_adapter", return_value=adapter) as factory:
                with patch.object(
                    manager, "_validate_codex_version", return_value="0.155.1"
                ):
                    manager.install(target, REPOSITORY_ROOT, False)

            factory.assert_called_once_with(target)
            self.assertTrue(adapter.arguments)
            for relative in adapter.arguments:
                with self.subTest(relative=relative):
                    self.assertEqual(relative, Path(relative).as_posix())
                    self.assertFalse(Path(relative).is_absolute())
                    self.assertNotIn("..", Path(relative).parts)

    def test_internal_factory_is_path_backed_and_raw_factory_selects_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = manager._repository_adapter(Path(directory))
            self.assertIs(type(adapter), manager.PathRepositoryAdapter)
            adapter.close()
            selected = manager._repository_adapter_from_raw_target(directory)
            try:
                if sys.platform == "win32":
                    self.assertIs(type(selected), manager.WindowsRepositoryAdapter)
                else:
                    self.assertIs(type(selected), manager.PathRepositoryAdapter)
            finally:
                selected.close()

    def test_transaction_and_lifecycle_functions_use_only_adapter_io_boundary(self) -> None:
        source = inspect.getsource(manager)
        tree = ast.parse(source)
        primitive_names = {
            "absolute",
            "chmod",
            "exists",
            "fsync",
            "is_file",
            "is_symlink",
            "link",
            "listdir",
            "lstat",
            "makedirs",
            "mkdir",
            "open",
            "read_bytes",
            "read_text",
            "readlink",
            "remove",
            "removedirs",
            "rename",
            "replace",
            "resolve",
            "rmdir",
            "scandir",
            "stat",
            "symlink",
            "touch",
            "unlink",
            "write_bytes",
            "write_text",
        }
        adapter_receivers = {"adapter", "repository"}
        observed: Counter[tuple[str, str]] = Counter()

        class Inventory(ast.NodeVisitor):
            def __init__(self) -> None:
                self.classes: list[str] = []
                self.functions: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.classes.append(node.name)
                self.generic_visit(node)
                self.classes.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Attribute) and node.func.attr in primitive_names:
                    receiver = node.func.value
                    while isinstance(receiver, ast.Attribute):
                        receiver = receiver.value
                    receiver_name = receiver.id if isinstance(receiver, ast.Name) else "<call>"
                    if receiver_name not in adapter_receivers:
                        scope_parts = [*self.classes, *self.functions]
                        observed[(".".join(scope_parts), node.func.attr)] += 1
                self.generic_visit(node)

        Inventory().visit(tree)
        approved = Counter(
            {
                ("PathRepositoryAdapter.observe_root_identity", "lstat"): 1,
                ("PathRepositoryAdapter.mkdir", "mkdir"): 1,
                ("WindowsRepositoryAdapter.exists", "exists"): 1,
                ("WindowsRepositoryAdapter.read_bytes", "read_bytes"): 1,
                ("WindowsRepositoryAdapter.hash_file", "exists"): 1,
                ("WindowsRepositoryAdapter.atomic_write", "mkdir"): 1,
                ("WindowsRepositoryAdapter.unlink", "unlink"): 1,
                ("WindowsRepositoryAdapter.rmdir", "rmdir"): 1,
                ("_sha256_file", "open"): 1,
                ("_text_hash", "replace"): 2,
                ("_assert_safe_target", "absolute"): 1,
                ("_assert_safe_target", "resolve"): 1,
                ("_lstat_optional", "lstat"): 1,
                ("_assert_no_link_components", "absolute"): 1,
                ("_assert_safe_path", "absolute"): 2,
                ("_safe_read_bytes", "read_bytes"): 1,
                ("_safe_unlink", "unlink"): 1,
                ("_safe_rmdir", "rmdir"): 1,
                ("_atomic_write", "mkdir"): 1,
                ("_atomic_write", "open"): 1,
                ("_atomic_write", "fsync"): 1,
                ("_atomic_write", "replace"): 1,
                ("_repository_adapter_from_raw_target", "absolute"): 1,
                ("_validate_sources", "is_file"): 1,
                ("_validate_sources", "is_symlink"): 1,
                ("_install_with_adapter", "read_bytes"): 1,
                ("main", "resolve"): 1,
            }
        )
        self.assertEqual(approved, observed)
        self.assertEqual(
            1,
            observed[("PathRepositoryAdapter.observe_root_identity", "lstat")],
        )
        self.assertEqual(1, observed[("_install_with_adapter", "read_bytes")])
        audited_boundary_scopes = {"_read_state", "_write_relative_json"}
        self.assertFalse(
            any(scope in audited_boundary_scopes for scope, _ in observed)
        )


if __name__ == "__main__":
    unittest.main()
