
// ==UserScript==
// @name         SupaArchive Pixiv
// @namespace    http://tampermonkey.net/
// @version      2024-02-29
// @description  try to take over the world!
// @author       You
// @match        https://www.pixiv.net/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=pixiv.net
// @run-at      document-idle
// ==/UserScript==

(function() {
    'use strict';

    document.head.appendChild(document.createElement("script")).src = "https://cdn.jsdelivr.net/npm/sweetalert2@11.10.5/dist/sweetalert2.all.min.js";

    let originalURL = window.location.href;

    setInterval(() => {
        if (window.location.href !== originalURL) {
            console.log(`${originalURL} -> ${window.location.href}`);
            originalURL = window.location.href;
            setTimeout(deployButton, 700);
        }
    });

    function deployButton() {
        if(document.getElementById("supaarchive-download-button")) {
            return;
        }

        let downloadButton = document.createElement("button");
        downloadButton.innerText = "Submit to SupaArchive";
        downloadButton.style.backgroundColor = "#0096fa";
        downloadButton.style.color = "white";
        downloadButton.style.paddingLeft = "16px";
        downloadButton.style.paddingRight = "16px";
        downloadButton.style.height = "32px";
        downloadButton.style.fontSize = "14px";
        downloadButton.style.borderRadius = "99999px";
        downloadButton.style.borderStyle = "none";
        downloadButton.style.marginRight = "12px";
        downloadButton.style.cursor = "pointer";
        downloadButton.id = "supaarchive-download-button";

        downloadButton.addEventListener("click", archivePage);
        let box = document.querySelector(".sc-181ts2x-0.gMEAWM") ;
        if (box) {
            box.appendChild(downloadButton);
        }

        console.log("Button deployed");
    }

    async function archivePage() {
        if(!window.location.href.startsWith("https://www.pixiv.net/en/artworks/")) {
            return;
        }

        let illustrationId = window.location.href.split("/").pop();
        // Remove the page number from the URL (anchor tag)
        if(illustrationId.includes("#")) {
            illustrationId = illustrationId.split("#")[0];
        }

        let illustrationTitle = document.querySelector("figcaption h1") ? document.querySelector("figcaption h1").innerText : "";
        let illustrationTags = Array.from(document.querySelectorAll("footer ul li span a")).reduce((accumulator, currentElement) => {
            accumulator.push(currentElement.childNodes[0].nodeValue.trim());
            return accumulator;
        }, []).map(tag => tag.replace(/ /g, "_"));
        let illustrationDescription = document.querySelector("figcaption p") ? document.querySelector("figcaption p").innerText : "";
        let authorName = document.querySelector("h2").innerText;
        let authorId = document.querySelector("h2 a").href.split("/").pop();

        let mangaPost = !!document.querySelector(".gtm-series-next-work-button-in-illust-detail");

        let pageRequest = await (await fetch(`/ajax/illust/${illustrationId}/pages?lang=en`)).json();
        let pageURLs = pageRequest.body.map(page => page.urls.original);



        fetch("http://127.0.0.1:8000/userscript/pixiv", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                "illustration_id": illustrationId,
                "title": illustrationTitle,
                "tags": illustrationTags,
                "description": illustrationDescription,
                "author_name": authorName,
                "author_id": authorId,
                "pages": pageURLs
            })
        }).then(response => response.json()).then(data => {
            Swal.fire({
                title: "Success",
                text: data.message,
                icon: "success",
                toast: true,
                position: "top-right",
                timer: 3000,
                timerProgressBar: true,
                showConfirmButton: false
            });
        }).catch(error => {
            Swal.fire({
                title: "Error",
                text: "An error occurred while submitting the artwork to SupaArchive.",
                icon: "error",
                toast: true,
                position: "top-right",
                timer: 3000,
                showConfirmButton: false
            });
        });
    }

    setTimeout(deployButton, 1000);
})();