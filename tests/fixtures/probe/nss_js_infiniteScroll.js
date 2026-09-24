var page = 0,
    inCallback = false,
    hasReachedEndOfInfiniteScroll = false,
    jsDebugConsoleLog = false;

function getDocumentHeight() {
    const body = document.body;
    const html = document.documentElement;

    return Math.max(
        body.scrollHeight, body.offsetHeight,
        html.clientHeight, html.scrollHeight, html.offsetHeight
    );
};

function getScrollTop() {
    return (window.pageYOffset !== undefined) ? window.pageYOffset : (document.documentElement || document.body.parentNode || document.body).scrollTop;
}

var scrollHandler = function () {
    var scrTop = $(window).scrollTop();
  //  var getScrTop = getScrollTop();
    var docHei = $(document).height();
  //  var getDocHei = getDocumentHeight();
  //  var winHei = $(window).height();
    var avaWinHei = screen.availHeight * 1.5;
  //  var cmpTip = $(document).outerHeight(true) - $(window).height();
    if (hasReachedEndOfInfiniteScroll == false &&
        (scrTop >= docHei - avaWinHei)) {
        loadMoreToInfiniteScrollTable(moreRowsUrl, currParams, currViewId, currSort);
    }
}

var ulScrollHandler = function () {
    if (hasReachedEndOfInfiniteScroll == false &&
        ($(window).scrollTop() == $(document).height() - $(window).height())) {
        loadMoreToInfiniteScrollUl(moreRowsUrl);
    }
}

function loadMoreToInfiniteScrollUl(loadMoreRowsUrl) {
    if (page > -1 && !inCallback) {
        inCallback = true;
        page++;
        spinon();
        $.ajax({
            type: 'GET',
            url: loadMoreRowsUrl,
            data: "pageNum=" + page,
            success: function (data, textstatus) {
                if (data != '') {
                    $("ul.infinite-scroll").append(data);
                }
                else {
                    page = -1;
                }

                inCallback = false;
                spinoff();
            },
            error: function (XMLHttpRequest, textStatus, errorThrown) {
            }
        });
    }
}

function loadMoreToInfiniteScrollTable(loadMoreRowsUrl, loadCurrParams, loadCurrViewId, loadCurrSort) {
    if (page > -1 && !inCallback) {
        inCallback = true;
        page++;
        spinon();
        if (jsDebugConsoleLog) {
            console.log('loadMoreRowsUrl:' + loadMoreRowsUrl);
            console.log('loadCurrParams:' + loadCurrParams);
            console.log('loadCurrViewId:' + loadCurrViewId);
            console.log('loadCurrSort:' + loadCurrSort)
        }
        $.ajax({
            type: 'POST',
            url: loadMoreRowsUrl,
            data: {
                vyhledavaciPodminky: loadCurrParams,
                zobrazeniVysledkuId: loadCurrViewId,
                pageNum: page,
                resultOrder: loadCurrSort
            },
            success: function (data, textstatus) {
                if (jsDebugConsoleLog) {
                    console.log('result recieved');
                }
                if ((data != '')&&(data.length > 5)) {
                    $("table.infinite-scroll").append(data);
                    $("table.infinite-scroll > tbody > tr:even").addClass("alt-row-class");
                    $("table.infinite-scroll > tbody > tr:odd").removeClass("alt-row-class");
                    if (jsDebugConsoleLog) {
                        console.log('result data added - data(' + data + ')');
                    }
                }
                else {
                    page = -1;
                    showNoMoreRecords();
                    if (jsDebugConsoleLog) {
                        console.log('result empty');
                    }
                }

                inCallback = false;
                spinoff();
            },
            error: function (XMLHttpRequest, textStatus, errorThrown) {
            }
        });
    }
}

function showNoMoreRecords() {
    hasReachedEndOfInfiniteScroll = true;
    if (jsDebugConsoleLog) {
        console.log('showNoMoreRecords page(' + page + ')');
    }
}