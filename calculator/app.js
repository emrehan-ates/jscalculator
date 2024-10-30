const display = document.getElementById("display");

var lastEnteredSign = false;

let getDisplayValue = () => display.innerText;
function appendNumber(num){
    if(getDisplayValue() === '0'){
        display.innerText = num;
        lastEnteredSign = false;
    } else {
        display.innerText = getDisplayValue()+num;
        lastEnteredSign = false;
    }
}

function chooseOperation(sign){
    if (lastEnteredSign){
        display.innerText = getDisplayValue().slice(0,-1) + sign;
        lastEnteredSign = true;
    } else {
    display.innerText = getDisplayValue() + sign;
    lastEnteredSign = true;
    }
}

function clearDisplay() {
    display.innerText = '0';
}

function calculateResult(){
    const expr = getDisplayValue();
    const res = eval(expr);

    let res2;
    
    res2 = Number.isInteger(res) ? res : res.toFixed(2);
    

    display.innerText = String(res2);
}