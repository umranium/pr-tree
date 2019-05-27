### Requirements

- virtualenv (see: [installation](https://virtualenv.pypa.io/en/latest/installation/))

### Installation

- Change to this folder
- Run `install.sh` to create a virtual environment and install required python packages in the environment:
```bash
./install.sh
```
- Add the folder to your path
```bash
echo "export PATH=PATH:$(pwd)" >> ~/.bash_profile
```
- export your github token
```bash
export GITHUB_TOKEN=abc
```

### Running

```bash
pr_tree -h
```
