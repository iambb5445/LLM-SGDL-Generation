This is some step for using setting up requirements to run `orchestrate.py`, specifically within `nautilus` optiputer. These are my personal notes to remember how to do this. If you would like to use this in your own work, use the appropriate namespace instead of `design-reasoning-lab`.

# Storage

Define storage with:
```bash
kubectl create -f k8s\pvc.yaml
```

# Jobs

Run jobs using
```bash
python orchestrate.py
```
the log files will be saved locally.

You may define a seed using `--seed` or use other options. Use `--help` to see all options.

# See files

Use the following command to get access into the PVC
```bash
kubectl apply -f pvc-browser.yaml
kubectl exec -it pvc-browser -n design-reasoning-lab -- sh
```

This will open a terminal that allows to view and browse the files using `ls` and 'cd'. When done, you can use `ctrl+D` to stop the terminal. You may use the command below to download any file or files:
```bash
kubectl cp design-reasoning-lab/pvc-browser:/path/to/remote/dir ./local/dir
```

And when done
```bash
kubectl delete pod pvc-browser -n design-reasoning-lab
```

# Download files

Use
```bash
# Copy a single file
kubectl cp design-reasoning-lab/pvc-browser:/mnt/results/1234567890/g0/evaluation.csv ./evaluation.csv

# Copy a whole directory
kubectl cp design-reasoning-lab/pvc-browser:/mnt/results/1234567890 ./results
```
whie the above command for viewing files is running and keeping pvc-browser alive