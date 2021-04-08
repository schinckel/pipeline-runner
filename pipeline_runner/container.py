import base64
import io
import logging
import os.path
import posixpath
import sys
import tarfile
import uuid
from logging import Logger
from time import time
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import boto3
import docker
from docker.models.containers import Container
from dotenv import dotenv_values

from . import utils
from .config import config
from .models import Image, Pipeline, Step

logger = logging.getLogger(__name__)


class ContainerRunner:
    def __init__(
        self,
        pipeline: Pipeline,
        step: Step,
        image: Image,
        name: str,
        data_volume_name: str,
        output_logger: Logger,
        mem_limit: int = 512,
        services_names: List[str] = None,
    ):
        self._pipeline = pipeline
        self._step = step
        self._image = image
        self._name = name
        self._data_volume_name = data_volume_name
        self._logger = output_logger
        self._mem_limit = mem_limit * 2 ** 20  # MiB to B
        self._services_names = services_names or []

        self._client = docker.from_env()
        self._container = None

    def start(self):
        pull_image(self._client, self._image)
        self._start_container()
        self._create_pipeline_directories()
        self._install_docker_client()

        return self._container

    def stop(self):
        if not self._container:
            return

        logger.info("Removing container: %s", self._container.name)
        self._container.remove(v=True, force=True)

    def run_script(
        self,
        script: Union[str, List[str]],
        user: Optional[Union[int, str]] = None,
        env: Optional[Dict[str, Any]] = None,
        exec_time: bool = False,
    ) -> int:
        command = utils.stringify(script, sep="\n")

        csr = ContainerScriptRunnerFactory.get(self._container, command, self._logger, user, env, exec_time)

        return csr.run()

    def run_command(
        self, command: Union[str, List[str]], wrap_in_shell: bool = True, user: Optional[Union[int, str]] = None
    ) -> Tuple[int, bytes]:
        command = utils.stringify(command)

        if wrap_in_shell:
            command = utils.wrap_in_shell(command)

        if user is not None:
            user = str(user)

        return self._container.exec_run(command, user=user)

    def path_exists(self, path) -> bool:
        ret, _ = self.run_command(f'[ -e "$(realpath "{path}")" ]')
        return ret == 0

    def get_archive(self, *args, **kwargs):
        return self._container.get_archive(*args, **kwargs)

    def put_archive(self, *args, **kwargs):
        return self._container.put_archive(*args, **kwargs)

    def _start_container(self):
        logger.info("Creating container: %s", self._name)

        self._container = self._client.containers.run(
            self._image.name,
            name=self._name,
            entrypoint="sh",
            user=self._image.run_as_user,
            working_dir=config.build_dir,
            environment=self._get_env_vars(),
            volumes=self._get_volumes(),
            mem_limit=self._mem_limit,
            network_mode="host",
            tty=True,
            detach=True,
        )

        logger.debug("Created container: %s", self._container.name)
        logger.debug("Image Used: %s", self._image.name)

    def _create_pipeline_directories(self):
        mkdir_cmd = " ".join(
            [
                "install",
                "-dD",
                "-o",
                str(self._image.run_as_user or 0),
                config.build_dir,
                config.scripts_dir,
                config.temp_dir,
                config.caches_dir,
            ]
        )
        exit_code, output = self.run_command(mkdir_cmd, user=0)
        if exit_code != 0:
            raise Exception(f"Error creating required directories: {output}")

    def _get_env_vars(self):
        env_vars = self._get_pipelines_env_vars()

        env_vars.update(dotenv_values(".env"))
        for env_file in config.env_files:
            env_vars.update(dotenv_values(env_file))

        return env_vars

    def _get_pipelines_env_vars(self) -> Dict[str, str]:
        project_slug = config.project_slug
        git_branch = utils.get_git_current_branch()
        git_commit = utils.get_git_current_commit()

        env_vars = {
            "BUILD_DIR": config.build_dir,
            "BITBUCKET_BRANCH": git_branch,
            "BITBUCKET_BUILD_NUMBER": self._pipeline.number,
            "BITBUCKET_CLONE_DIR": config.build_dir,
            "BITBUCKET_COMMIT": git_commit,
            "BITBUCKET_PROJECT_KEY": "PR",
            "BITBUCKET_REPO_FULL_NAME": f"{project_slug}/{project_slug}",
            "BITBUCKET_REPO_IS_PRIVATE": "true",
            "BITBUCKET_REPO_OWNER": config.username,
            "BITBUCKET_REPO_OWNER_UUID": config.owner_uuid,
            "BITBUCKET_REPO_SLUG": project_slug,
            "BITBUCKET_REPO_UUID": config.repo_uuid,
            "BITBUCKET_WORKSPACE": project_slug,
        }

        if self._step.deployment:
            env_vars["BITBUCKET_DEPLOYMENT_ENVIRONMENT"] = self._step.deployment

        if "docker" in self._services_names:
            env_vars["DOCKER_HOST"] = "tcp://localhost:2375"

        return env_vars

    def _get_volumes(self):
        return {
            config.project_directory: {"bind": config.remote_workspace_dir, "mode": "ro"},
            self._data_volume_name: {"bind": config.remote_pipeline_dir},
        }

    def _install_docker_client(self):
        if "docker" not in self._services_names:
            return

        cmd = """
        if type apt-get >/dev/null 2>&1; then
            export DEBIAN_FRONTEND=noninteractive
            apt-get update && apt-get install -y --no-install-recommends docker.io
        elif type apk >/dev/null 2>&1; then
            apk add --no-cache docker-cli
        else
            echo "Unsupported distribution" >&2
            exit 1
        fi
        """

        if self.run_script(cmd, user=0) != 0:
            raise Exception("Error installing necessary binaries")


class ContainerScriptRunner:
    def __init__(
        self,
        container: Container,
        script: str,
        output_logger: Optional[Logger] = None,
        user: Optional[Union[int, str]] = None,
        env: Optional[Dict[str, Any]] = None,
    ):
        self._container = container
        self._script = script
        self._logger = output_logger
        self._user = str(user) if user is not None else None
        self._env = env or {}

        if self._logger:

            def stdout_print(msg):
                self._logger.info(msg)

            def stderr_print(msg):
                self._logger.error(msg)

        else:

            def stdout_print(msg):
                print(msg, end="")

            def stderr_print(msg):
                print(msg, end="", file=sys.stderr)

        self._stdout_print = stdout_print
        self._stderr_print = stderr_print

    def run(self) -> int:
        entrypoint, exit_code_file_path = self._prepare_script_for_remote_execution()

        self._execute_script_on_container(entrypoint)
        return self._get_exit_code_of_command(exit_code_file_path)

    def _prepare_script_for_remote_execution(self) -> Tuple[str, str]:
        script = self._add_traces_to_script(self._script)
        exit_code_file_path = posixpath.join(config.temp_dir, f"exit_code-{uuid.uuid4().hex}")

        sh_script_name = f"shell_script-{uuid.uuid4().hex}.sh"
        sh_script_path = posixpath.join(config.scripts_dir, sh_script_name)
        sh_script = self._wrap_script_in_posix_shell(script)

        bash_script_name = f"bash_script-{uuid.uuid4().hex}.sh"
        bash_script_path = posixpath.join(config.scripts_dir, bash_script_name)
        bash_script = self._wrap_script_in_bash(script)

        wrapper_script_name = f"wrapper_script-{uuid.uuid4().hex}.sh"
        wrapper_script_path = posixpath.join(config.scripts_dir, wrapper_script_name)
        wrapper_script = self._make_wrapper_script(sh_script_path, bash_script_path, exit_code_file_path)

        scripts = (
            (sh_script_name, sh_script),
            (bash_script_name, bash_script),
            (wrapper_script_name, wrapper_script),
        )

        self._upload_to_container(scripts)

        return wrapper_script_path, exit_code_file_path

    def _execute_script_on_container(self, entrypoint):
        _, output_stream = self._container.exec_run(
            ["/bin/sh", entrypoint], user=self._user, tty=True, stream=True, demux=True, environment=self._env
        )

        self._print_execution_log(output_stream)

    def _print_execution_log(self, output_stream):
        for stdout, stderr in output_stream:
            if stdout:
                self._stdout_print(stdout.decode())
            if stderr:
                self._stderr_print(stderr.decode())

        self._stdout_print("\n")

    def _get_exit_code_of_command(self, exit_code_file_path: str) -> int:
        meta_exit_code, output = self._container.exec_run(["/bin/cat", exit_code_file_path])
        if meta_exit_code != 0:
            raise Exception(f"Error getting command exit code: {output.decode()}")

        str_code = output.decode().strip()

        try:
            exit_code = int(str_code)
        except ValueError:
            raise Exception(f"Invalid exit code: {str_code}")
        else:
            return exit_code

    def _add_traces_to_script(self, script):
        script_lines = map(self._add_trace_to_script_line, script.split("\n"))

        return '\nprintf "\\n"\n'.join(line for line in script_lines if line)

    def _add_trace_to_script_line(self, line):
        line = line.strip()
        if not line:
            return None

        return f"{self._add_group_separator(line)}\n{line}"

    @staticmethod
    def _add_group_separator(value):
        value = utils.escape_shell_string(value)

        return f'printf "\\x1d+ {value}\\n"'

    @staticmethod
    def _wrap_script_in_posix_shell(script):
        return "\n".join(["#! /bin/sh", "set -e", script])

    @staticmethod
    def _wrap_script_in_bash(script):
        return "\n".join(["#! /bin/bash", "set -e", "set +H", script])

    @staticmethod
    def _make_wrapper_script(sh_script_path, bash_script_path, exit_code_file_path):
        return "\n".join(
            [
                "#! /bin/sh",
                "if [ -f /bin/bash ]; then",
                f"    /bin/bash -i {bash_script_path}",
                f"    echo $? > {exit_code_file_path}",
                "    exit $?",
                "else",
                f"    /bin/sh {sh_script_path}",
                f"    echo $? > {exit_code_file_path}",
                "    exit $?",
                "fi",
            ]
        )

    def _upload_to_container(self, scripts: Iterable[Tuple[str, str]]):
        tar_data = io.BytesIO()

        with tarfile.open(fileobj=tar_data, mode="w|") as tar:
            for (name, script) in scripts:
                ti = tarfile.TarInfo(name)
                script_data = script.encode()
                ti.size = len(script_data)
                ti.mode = 0o644

                tar.addfile(ti, io.BytesIO(script_data))

        res = self._container.put_archive(config.scripts_dir, tar_data.getvalue())
        if not res:
            raise Exception("Error uploading scripts to container")


class ContainerScriptRunnerWithExecTime(ContainerScriptRunner):
    def __init__(
        self,
        container: Container,
        script: str,
        output_logger: Optional[Logger] = None,
        user: Optional[Union[int, str]] = None,
        env: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(container, script, output_logger, user, env)
        self._timestamp = None

    def _print_execution_log(self, output_stream):
        for stdout, stderr in output_stream:
            if stdout:
                chunks = iter(stdout.decode().split("\x1d"))

                self._stdout_print(next(chunks))

                for c in chunks:
                    self._print_timing()
                    self._stdout_print(c)
            if stderr:
                self._stderr_print(stderr.decode())

        self._print_timing()

    def _print_timing(self):
        now = time()
        if self._timestamp:
            self._stdout_print(f"\n>>> Execution time: {now - self._timestamp:.3f}s\n\n")

        self._timestamp = now


class ContainerScriptRunnerFactory:
    @staticmethod
    def get(
        container: Container,
        script: str,
        output_logger: Optional[Logger] = None,
        user: Optional[Union[int, str]] = None,
        env: Optional[Dict[str, Any]] = None,
        exec_time: bool = False,
    ):
        if exec_time:
            cls = ContainerScriptRunnerWithExecTime
        else:
            cls = ContainerScriptRunner

        return cls(container, script, output_logger, user, env)


_pulled_images = set()


def pull_image(client, image):
    global _pulled_images

    if image.name in _pulled_images:
        logger.info("Image already pulled: %s", image.name)
        return

    logger.info("Pulling image: %s", image.name)

    auth_config = get_image_authentication(image)
    client.images.pull(image.name, auth_config=auth_config)

    _pulled_images.add(image.name)


def get_image_authentication(image: Image):
    if image.aws:
        aws_access_key_id = image.aws["access-key"]
        aws_secret_access_key = image.aws["secret-key"]
        aws_session_token = os.getenv("AWS_SESSION_TOKEN")

        client = boto3.client(
            "ecr",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            region_name="ca-central-1",
        )

        resp = client.get_authorization_token()

        credentials = base64.b64decode(resp["authorizationData"][0]["authorizationToken"]).decode()
        username, password = credentials.split(":", maxsplit=1)

        return {
            "username": username,
            "password": password,
        }

    if image.username and image.password:
        return {
            "username": image.username,
            "password": image.password,
        }

    return None
