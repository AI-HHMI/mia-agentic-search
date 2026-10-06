# demo

Output folder for the `/download-dataset` skill while the real storage location is being decided.
Layout follows [AI-HHMI/miao#13](https://github.com/AI-HHMI/miao/issues/13):

```
demo/data/{modality}-{organism}-{name}/
├── manifest.json                 crop -> catalog record, source files, label layers
└── crop-NNN.zarr/
    ├── raw/                      OME-NGFF multiscales (TensorSwitch preset miaai)
    └── labels/{provenance}-{label_class}-{info}/zarr.json   label metadata
```

`demo/data/` and `demo/staging/` (downloads in progress) are git-ignored.
